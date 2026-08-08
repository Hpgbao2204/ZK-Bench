use bulletproofs::{BulletproofGens, PedersenGens, RangeProof};
use curve25519_dalek::scalar::Scalar;
use merlin::Transcript;
use rand_chacha::ChaChaRng;
use rand_core::SeedableRng;
use std::collections::BTreeMap;
use std::error::Error;
use zkbench_adapter_sdk::{
    AdapterRequest, AdapterResult, PhaseEvent, PhaseTimer, SCHEMA_VERSION, emit, emit_result,
    read_request_from_stdin,
};

const ADAPTER: &str = "bulletproofs-5.0.0-ristretto";
const RANGE_WORKLOAD: &str = "private_swap";

fn seed_bytes(seed: u64) -> [u8; 32] {
    let mut bytes = [0_u8; 32];
    for (index, chunk) in bytes.chunks_exact_mut(8).enumerate() {
        chunk.copy_from_slice(&seed.wrapping_add(index as u64 * 0x9e37_79b9).to_le_bytes());
    }
    bytes
}

fn numeric_parameter(
    request: &AdapterRequest,
    name: &str,
    default: usize,
) -> Result<usize, String> {
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

fn range_bits(request: &AdapterRequest) -> Result<usize, String> {
    let bits = numeric_parameter(request, "range_bits", 32)?;
    if !(8..=32).contains(&bits) || !bits.is_power_of_two() {
        return Err("range_bits must be one of 8, 16, or 32".to_owned());
    }
    Ok(bits)
}

fn aggregate_values(request: &AdapterRequest) -> Result<usize, String> {
    let scale =
        usize::try_from(request.scale).map_err(|_| "scale does not fit usize".to_owned())?;
    let count = scale
        .checked_mul(2)
        .ok_or_else(|| "aggregation size overflow".to_owned())?;
    if !count.is_power_of_two() {
        return Err("Bulletproofs aggregation requires a power-of-two value count".to_owned());
    }
    Ok(count)
}

fn values(request: &AdapterRequest, bits: usize, count: usize) -> Vec<u64> {
    let max = (1_u64 << bits) - 1;
    (0..count)
        .map(|index| {
            let mixed = request
                .seed
                .wrapping_add(index as u64 * 0x9e37_79b9)
                .rotate_left((index % 31) as u32);
            2 + mixed % (max - 2)
        })
        .collect()
}

fn fixture_digest(request: &AdapterRequest, values: &[u64]) -> f64 {
    let mut digest = 14_695_981_039_346_656_037_u64;
    let mut bytes = request.workload.as_bytes().to_vec();
    for (name, value) in &request.parameters {
        bytes.extend_from_slice(name.as_bytes());
        bytes.extend_from_slice(value.to_string().as_bytes());
    }
    for value in values {
        bytes.extend_from_slice(&value.to_le_bytes());
    }
    for byte in bytes {
        digest ^= u64::from(byte);
        digest = digest.wrapping_mul(1_099_511_628_211_u64);
    }
    (digest & ((1_u64 << 52) - 1)).max(2) as f64
}

fn emit_unsupported(request: &AdapterRequest, phase: &str, reason: &str) -> Result<(), String> {
    emit(&PhaseEvent::unsupported(request, ADAPTER, phase, reason))
}

fn run(request: &AdapterRequest) -> Result<(), Box<dyn Error>> {
    if request.workload != RANGE_WORKLOAD {
        return Err(format!(
            "{ADAPTER} is a specialized range-proof baseline; expected {RANGE_WORKLOAD}"
        )
        .into());
    }
    let bits = range_bits(request)?;
    let count = aggregate_values(request)?;

    let generated = PhaseTimer::start();
    let values = values(request, bits, count);
    let digest = fixture_digest(request, &values);
    emit(&PhaseEvent::measured(
        request,
        ADAPTER,
        "native_execution",
        generated.elapsed(),
        BTreeMap::from([
            ("application_units".to_owned(), request.scale as f64),
            ("aggregate_values".to_owned(), count as f64),
            ("range_bits".to_owned(), bits as f64),
            ("fixture_digest".to_owned(), digest),
        ]),
    )?)?;

    emit_unsupported(
        request,
        "setup_or_preprocess",
        "Bulletproofs range generators are fixed and setup-free",
    )?;
    emit_unsupported(
        request,
        "key_load",
        "the range-proof API has no serialized proving or verification key",
    )?;

    let witness_timer = PhaseTimer::start();
    let mut rng = ChaChaRng::from_seed(seed_bytes(request.seed));
    let blindings: Vec<Scalar> = (0..count).map(|_| Scalar::random(&mut rng)).collect();
    emit(&PhaseEvent::measured(
        request,
        ADAPTER,
        "witness",
        witness_timer.elapsed(),
        BTreeMap::from([
            ("aggregate_values".to_owned(), count as f64),
            ("range_bits".to_owned(), bits as f64),
        ]),
    )?)?;

    let pc_gens = PedersenGens::default();
    let bp_gens = BulletproofGens::new(bits, count);
    let prove_timer = PhaseTimer::start();
    let mut prove_transcript = Transcript::new(b"zkbench-bulletproofs-range-v1");
    let (proof, commitments) = RangeProof::prove_multiple_with_rng(
        &bp_gens,
        &pc_gens,
        &mut prove_transcript,
        &values,
        &blindings,
        bits,
        &mut rng,
    )?;
    emit(&PhaseEvent::measured(
        request,
        ADAPTER,
        "prove_total",
        prove_timer.elapsed(),
        BTreeMap::from([
            ("aggregate_values".to_owned(), count as f64),
            ("range_bits".to_owned(), bits as f64),
        ]),
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

    let mut verification_commitments = commitments.clone();
    if request.invalid_case.is_some() {
        let first = verification_commitments
            .first_mut()
            .ok_or("range proof produced no commitments")?;
        let point = first.decompress().ok_or("invalid commitment encoding")?;
        *first = (point + pc_gens.B).compress();
    }
    let verify_timer = PhaseTimer::start();
    let mut verify_transcript = Transcript::new(b"zkbench-bulletproofs-range-v1");
    let verify_result = proof.verify_multiple(
        &bp_gens,
        &pc_gens,
        &mut verify_transcript,
        &verification_commitments,
        bits,
    );
    let verify_ok = verify_result.is_ok();
    let verify_elapsed = verify_timer.elapsed();
    emit(&PhaseEvent::measured(
        request,
        ADAPTER,
        "verify_core",
        verify_elapsed,
        BTreeMap::from([("aggregate_values".to_owned(), count as f64)]),
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
            BTreeMap::from([("aggregate_values".to_owned(), count as f64)]),
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
        public_inputs: count as u64,
        constraints: (count * bits) as u64,
        relation_unit: "range_bits".to_owned(),
        invalid_case: request.invalid_case.clone(),
        error_type: None,
    })?;
    Ok(())
}

fn main() -> Result<(), Box<dyn Error>> {
    let request = read_request_from_stdin().map_err(|error| format!("request error: {error}"))?;
    run(&request)
}
