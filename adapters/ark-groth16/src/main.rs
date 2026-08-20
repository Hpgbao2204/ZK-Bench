use ark_bn254::{Bn254, Fr};
use ark_crypto_primitives::crh::sha256::{Sha256, constraints::Sha256Gadget, digest::Digest};
use ark_ff::{Field, ToConstraintField};
use ark_groth16::{Groth16, Proof, prepare_verifying_key};
use ark_r1cs_std::{alloc::AllocVar, eq::EqGadget, uint8::UInt8};
use ark_relations::{
    gr1cs::{ConstraintSynthesizer, ConstraintSystem, ConstraintSystemRef, SynthesisError},
    lc,
};
use ark_serialize::{CanonicalDeserialize, CanonicalSerialize};
use ark_std::rand::{SeedableRng, rngs::StdRng};
use rayon::ThreadPoolBuilder;
use std::collections::BTreeMap;
use std::error::Error;
use std::sync::OnceLock;
use std::time::Duration;
use zkbench_adapter_sdk::{
    AdapterRequest, AdapterResult, PhaseEvent, PhaseTimer, SCHEMA_VERSION, emit, emit_result,
    read_request_from_stdin,
};

mod relations;

const ADAPTER: &str = "ark-groth16-0.6.0-bn254";
const CONTROLLED_WORKLOAD: &str = "controlled_kernel";
const SHA256_WORKLOAD: &str = "sha256";
static RAYON_THREADS: OnceLock<Result<usize, String>> = OnceLock::new();

#[derive(Clone)]
struct MultiplicativeChain {
    initial: Option<Fr>,
    factor: Option<Fr>,
    output: Option<Fr>,
    steps: usize,
}

#[derive(Clone)]
struct Sha256Circuit {
    message: Option<Vec<u8>>,
    digest: Option<Vec<u8>>,
    message_len: usize,
}

impl ConstraintSynthesizer<Fr> for Sha256Circuit {
    fn generate_constraints(self, cs: ConstraintSystemRef<Fr>) -> Result<(), SynthesisError> {
        let message = self.message.unwrap_or_else(|| vec![0_u8; self.message_len]);
        let digest = self.digest.unwrap_or_else(|| vec![0_u8; 32]);
        let message_vars = message
            .iter()
            .map(|byte| UInt8::new_witness(cs.clone(), || Ok(*byte)))
            .collect::<Result<Vec<_>, _>>()?;
        let expected_vars = UInt8::new_input_vec(cs.clone(), &digest)?;
        let digest_vars = Sha256Gadget::<Fr>::digest(&message_vars)?;
        digest_vars
            .0
            .iter()
            .zip(expected_vars.iter())
            .try_for_each(|(actual, expected)| actual.enforce_equal(expected))?;
        Ok(())
    }
}

impl ConstraintSynthesizer<Fr> for MultiplicativeChain {
    fn generate_constraints(self, cs: ConstraintSystemRef<Fr>) -> Result<(), SynthesisError> {
        let factor_var =
            cs.new_input_variable(|| self.factor.ok_or(SynthesisError::AssignmentMissing))?;
        let output_var =
            cs.new_input_variable(|| self.output.ok_or(SynthesisError::AssignmentMissing))?;
        let mut current_value = self.initial;
        let mut current_var =
            cs.new_witness_variable(|| self.initial.ok_or(SynthesisError::AssignmentMissing))?;

        for index in 0..self.steps {
            let next_value = match (current_value, self.factor) {
                (Some(current), Some(factor)) => Some(current * factor),
                _ => None,
            };
            let next_var = if index + 1 == self.steps {
                output_var
            } else {
                cs.new_witness_variable(|| next_value.ok_or(SynthesisError::AssignmentMissing))?
            };
            cs.enforce_r1cs_constraint(
                || lc!() + current_var,
                || lc!() + factor_var,
                || lc!() + next_var,
            )?;
            current_value = next_value;
            current_var = next_var;
        }
        Ok(())
    }
}

#[derive(Clone)]
enum BenchmarkCircuit {
    Controlled(MultiplicativeChain),
    Sha256(Sha256Circuit),
    Application(relations::ApplicationCircuit),
}

impl ConstraintSynthesizer<Fr> for BenchmarkCircuit {
    fn generate_constraints(self, cs: ConstraintSystemRef<Fr>) -> Result<(), SynthesisError> {
        match self {
            Self::Controlled(circuit) => circuit.generate_constraints(cs),
            Self::Sha256(circuit) => circuit.generate_constraints(cs),
            Self::Application(circuit) => circuit.generate_constraints(cs),
        }
    }
}

struct BenchmarkPlan {
    circuit: BenchmarkCircuit,
    setup_circuit: BenchmarkCircuit,
    public_inputs: Vec<Fr>,
    native_units: u64,
    profile: BTreeMap<String, f64>,
}

struct RunOutcome {
    proof_bytes: usize,
    constraints: usize,
    native_units: u64,
    public_inputs: usize,
    verify_ok: bool,
}

fn measured_duration(
    request: &AdapterRequest,
    phase: &str,
    elapsed: Duration,
    metrics: BTreeMap<String, f64>,
) -> Result<(), String> {
    emit(&PhaseEvent::measured(
        request, ADAPTER, phase, elapsed, metrics,
    )?)
}

fn measured_event(
    request: &AdapterRequest,
    phase: &str,
    timer: &PhaseTimer,
    metrics: BTreeMap<String, f64>,
) -> Result<(), String> {
    measured_duration(request, phase, timer.elapsed(), metrics)
}

fn unsupported_events(request: &AdapterRequest) -> Result<(), String> {
    let reason = "ark-groth16 does not expose stable per-run phase hooks";
    for phase in ["fft_ntt", "msm", "commitment", "key_load"] {
        emit(&PhaseEvent::unsupported(request, ADAPTER, phase, reason))?;
    }
    Ok(())
}

fn values(request: &AdapterRequest) -> (Fr, Fr, Fr) {
    let initial = Fr::from(request.seed.wrapping_add(17));
    let factor = Fr::from(request.seed.wrapping_add(29));
    let mut output = initial;
    for _ in 0..request.scale {
        output *= factor;
    }
    (initial, factor, output)
}

fn sha256_values(request: &AdapterRequest) -> (Vec<u8>, Vec<u8>) {
    let message = (0..request.scale)
        .map(|index| {
            request
                .seed
                .wrapping_add(index)
                .rotate_left((index % 8) as u32) as u8
        })
        .collect::<Vec<_>>();
    let digest = Sha256::digest(&message).to_vec();
    (message, digest)
}

fn packed_public_inputs(bytes: &[u8]) -> Vec<Fr> {
    bytes
        .to_field_elements()
        .expect("BN254 should pack byte vectors into field elements")
}

fn build_plan(request: &AdapterRequest) -> Result<BenchmarkPlan, Box<dyn Error>> {
    if request.workload == CONTROLLED_WORKLOAD {
        if !request.parameters.is_empty() {
            return Err("controlled_kernel does not accept workload parameters".into());
        }
        let steps = usize::try_from(request.scale)?;
        let (initial, factor, output) = values(request);
        return Ok(BenchmarkPlan {
            circuit: BenchmarkCircuit::Controlled(MultiplicativeChain {
                initial: Some(initial),
                factor: Some(factor),
                output: Some(output),
                steps,
            }),
            setup_circuit: BenchmarkCircuit::Controlled(MultiplicativeChain {
                initial: None,
                factor: None,
                output: None,
                steps,
            }),
            public_inputs: vec![factor, output],
            native_units: request.scale,
            profile: BTreeMap::from([("application_units".to_owned(), request.scale as f64)]),
        });
    }
    if request.workload == SHA256_WORKLOAD {
        if !request.parameters.is_empty() {
            return Err(
                "sha256 does not accept workload parameters; scale is message bytes".into(),
            );
        }
        let message_len = usize::try_from(request.scale)?;
        let (message, digest) = sha256_values(request);
        return Ok(BenchmarkPlan {
            circuit: BenchmarkCircuit::Sha256(Sha256Circuit {
                message: Some(message.clone()),
                digest: Some(digest.clone()),
                message_len,
            }),
            setup_circuit: BenchmarkCircuit::Sha256(Sha256Circuit {
                message: None,
                digest: None,
                message_len,
            }),
            public_inputs: packed_public_inputs(&digest),
            native_units: request.scale,
            profile: BTreeMap::from([("message_bytes".to_owned(), request.scale as f64)]),
        });
    }
    if relations::supports(&request.workload) {
        let plan = relations::build_plan(request)?;
        return Ok(BenchmarkPlan {
            circuit: BenchmarkCircuit::Application(plan.circuit),
            setup_circuit: BenchmarkCircuit::Application(plan.setup_circuit),
            public_inputs: plan.public_inputs,
            native_units: plan.native_units,
            profile: plan.profile,
        });
    }
    Err(format!(
        "unsupported workload {}; expected controlled_kernel, sha256, credential, \
         batched_state, or private_swap",
        request.workload
    )
    .into())
}

fn configure_rayon(threads: usize) -> Result<(), String> {
    let configured = RAYON_THREADS.get_or_init(|| {
        ThreadPoolBuilder::new()
            .num_threads(threads)
            .build_global()
            .map(|_| threads)
            .map_err(|error| format!("failed to configure Rayon: {error}"))
    });
    match configured {
        Ok(configured_threads) if *configured_threads == threads => Ok(()),
        Ok(configured_threads) => Err(format!(
            "adapter process already uses {configured_threads} Rayon threads; \
             launch one process per thread configuration"
        )),
        Err(error) => Err(error.clone()),
    }
}

fn run(request: &AdapterRequest) -> Result<RunOutcome, Box<dyn Error>> {
    configure_rayon(request.threads)?;

    let native_timer = PhaseTimer::start();
    let plan = build_plan(request)?;
    let mut native_metrics = plan.profile.clone();
    native_metrics.insert("native_work_units".to_owned(), plan.native_units as f64);
    if request.workload != CONTROLLED_WORKLOAD && request.workload != SHA256_WORKLOAD {
        native_metrics.insert(
            "relation_digest".to_owned(),
            relations::relation_digest(request)?,
        );
    }
    measured_event(request, "native_execution", &native_timer, native_metrics)?;

    let witness_timer = PhaseTimer::start();
    let cs = ConstraintSystem::<Fr>::new_ref();
    plan.circuit.clone().generate_constraints(cs.clone())?;
    let constraints = cs.num_constraints();
    if !cs.is_satisfied()? {
        return Err(format!("{} witness does not satisfy R1CS", request.workload).into());
    }
    let mut witness_metrics = plan.profile.clone();
    witness_metrics.insert("constraint_count".to_owned(), constraints as f64);
    measured_event(request, "witness", &witness_timer, witness_metrics)?;

    let setup_timer = PhaseTimer::start();
    let mut setup_rng = StdRng::seed_from_u64(request.seed ^ 0xA11C_E5E7);
    let proving_key = Groth16::<Bn254>::generate_random_parameters_with_reduction(
        plan.setup_circuit,
        &mut setup_rng,
    )?;
    let processed_vk = prepare_verifying_key(&proving_key.vk);
    measured_event(
        request,
        "setup_or_preprocess",
        &setup_timer,
        BTreeMap::from([("constraint_count".to_owned(), constraints as f64)]),
    )?;

    let prove_timer = PhaseTimer::start();
    let mut proof_rng = StdRng::seed_from_u64(request.seed ^ 0xBADC_0FFE);
    let proof = Groth16::<Bn254>::create_random_proof_with_reduction(
        plan.circuit,
        &proving_key,
        &mut proof_rng,
    )?;
    measured_event(
        request,
        "prove_total",
        &prove_timer,
        BTreeMap::from([("constraint_count".to_owned(), constraints as f64)]),
    )?;

    let serialize_timer = PhaseTimer::start();
    let mut proof_buffer = Vec::new();
    proof.serialize_compressed(&mut proof_buffer)?;
    measured_event(
        request,
        "serialize",
        &serialize_timer,
        BTreeMap::from([("proof_bytes".to_owned(), proof_buffer.len() as f64)]),
    )?;

    let verify_total_timer = PhaseTimer::start();
    let deserialize_timer = PhaseTimer::start();
    let decoded_proof = Proof::<Bn254>::deserialize_compressed(proof_buffer.as_slice())?;
    let deserialize_elapsed = deserialize_timer.elapsed();

    let mut public_inputs = plan.public_inputs;
    if request.invalid_case.as_deref() == Some("wrong_public_input") {
        let last = public_inputs
            .last_mut()
            .ok_or("benchmark relation must expose public inputs")?;
        *last += Fr::ONE;
    } else if request.invalid_case.is_some() {
        return Err(format!(
            "unsupported invalid case: {}",
            request.invalid_case.as_deref().unwrap_or_default()
        )
        .into());
    }
    let verify_core_timer = PhaseTimer::start();
    let verify_ok = Groth16::<Bn254>::verify_proof(&processed_vk, &decoded_proof, &public_inputs)?;
    let verify_core_elapsed = verify_core_timer.elapsed();
    let verify_total_elapsed = verify_total_timer.elapsed();
    measured_duration(
        request,
        "deserialize",
        deserialize_elapsed,
        BTreeMap::from([("proof_bytes".to_owned(), proof_buffer.len() as f64)]),
    )?;
    measured_duration(request, "verify_core", verify_core_elapsed, BTreeMap::new())?;
    if request.invalid_case.is_some() {
        measured_duration(request, "invalid_reject", verify_core_elapsed, BTreeMap::new())?;
    }
    measured_duration(
        request,
        "verify_total",
        verify_total_elapsed,
        BTreeMap::new(),
    )?;
    unsupported_events(request)?;

    Ok(RunOutcome {
        proof_bytes: proof_buffer.len(),
        constraints,
        native_units: plan.native_units,
        public_inputs: public_inputs.len(),
        verify_ok,
    })
}

fn main() {
    if let Err(error) = real_main() {
        eprintln!("{ADAPTER}: {error}");
        std::process::exit(1);
    }
}

fn real_main() -> Result<(), Box<dyn Error>> {
    let request = read_request_from_stdin()?;
    let outcome = run(&request)?;
    emit_result(&AdapterResult {
        schema_version: SCHEMA_VERSION,
        event_type: "result",
        run_id: request.run_id.clone(),
        adapter: ADAPTER.to_owned(),
        verify_ok: outcome.verify_ok,
        proof_bytes: u64::try_from(outcome.proof_bytes)?,
        native_work_units: outcome.native_units,
        public_inputs: u64::try_from(outcome.public_inputs)?,
        constraints: u64::try_from(outcome.constraints)?,
        relation_unit: "r1cs_constraints".to_owned(),
        invalid_case: request.invalid_case.clone(),
        error_type: if outcome.verify_ok {
            None
        } else {
            Some("cryptographic_rejection".to_owned())
        },
    })?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn valid_and_invalid_public_inputs() {
        let valid = AdapterRequest {
            run_id: "valid".to_owned(),
            workload: "controlled_kernel".to_owned(),
            scale: 8,
            threads: 2,
            seed: 7,
            mode: "warm".to_owned(),
            invalid_case: None,
            parameters: BTreeMap::new(),
        };
        let valid_outcome = run(&valid).unwrap();
        assert!(valid_outcome.verify_ok);
        assert_eq!(valid_outcome.constraints, 8);
        assert!(valid_outcome.proof_bytes > 1);

        let mut invalid = valid;
        invalid.run_id = "invalid".to_owned();
        invalid.invalid_case = Some("wrong_public_input".to_owned());
        let invalid_outcome = run(&invalid).unwrap();
        assert!(!invalid_outcome.verify_ok);
    }

    #[test]
    fn sha256_vector_proves_and_rejects_wrong_digest() {
        let request = AdapterRequest {
            run_id: "sha-valid".to_owned(),
            workload: SHA256_WORKLOAD.to_owned(),
            scale: 32,
            threads: 2,
            seed: 19,
            mode: "warm".to_owned(),
            invalid_case: None,
            parameters: BTreeMap::new(),
        };
        let valid = run(&request).unwrap();
        assert!(valid.verify_ok);
        assert!(valid.constraints > 1);
        assert!(valid.public_inputs > 1);

        let mut invalid = request;
        invalid.run_id = "sha-invalid".to_owned();
        invalid.invalid_case = Some("wrong_public_input".to_owned());
        assert!(!run(&invalid).unwrap().verify_ok);
    }

    #[test]
    fn private_swap_proves_and_rejects_wrong_public_input() {
        let request = AdapterRequest {
            run_id: "swap-valid".to_owned(),
            workload: "private_swap".to_owned(),
            scale: 2,
            threads: 2,
            seed: 11,
            mode: "warm".to_owned(),
            invalid_case: None,
            parameters: BTreeMap::from([
                ("hash_rounds".to_owned(), 5_u64.into()),
                ("range_bits".to_owned(), 32_u64.into()),
                ("time_bits".to_owned(), 16_u64.into()),
                ("merkle_depth".to_owned(), 4_u64.into()),
                ("membership_paths".to_owned(), 2_u64.into()),
                ("ablation".to_owned(), "full".into()),
            ]),
        };
        let valid = run(&request).unwrap();
        assert!(valid.verify_ok);
        assert!(valid.constraints > 2);
        assert!(valid.public_inputs > 2);

        let mut invalid = request;
        invalid.run_id = "swap-invalid".to_owned();
        invalid.invalid_case = Some("wrong_public_input".to_owned());
        assert!(!run(&invalid).unwrap().verify_ok);
    }

    #[test]
    fn rejects_unknown_workload() {
        let request = AdapterRequest {
            run_id: "wrong-workload".to_owned(),
            workload: "unknown_workload".to_owned(),
            scale: 8,
            threads: 2,
            seed: 7,
            mode: "warm".to_owned(),
            invalid_case: None,
            parameters: BTreeMap::new(),
        };
        assert!(run(&request).is_err());
    }
}
