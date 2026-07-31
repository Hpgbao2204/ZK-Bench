use ark_bn254::{Bn254, Fr};
use ark_ff::Field;
use ark_groth16::{Groth16, Proof, prepare_verifying_key};
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

const ADAPTER: &str = "ark-groth16-0.6.0-bn254";
const WORKLOAD: &str = "controlled_kernel";
static RAYON_THREADS: OnceLock<usize> = OnceLock::new();

#[derive(Clone)]
struct MultiplicativeChain {
    initial: Option<Fr>,
    factor: Option<Fr>,
    output: Option<Fr>,
    steps: usize,
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

struct RunOutcome {
    proof_bytes: usize,
    constraints: usize,
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

fn configure_rayon(threads: usize) -> Result<(), String> {
    if let Some(configured) = RAYON_THREADS.get() {
        return if *configured == threads {
            Ok(())
        } else {
            Err(format!(
                "adapter process already uses {configured} Rayon threads; \
                 launch one process per thread configuration"
            ))
        };
    }
    ThreadPoolBuilder::new()
        .num_threads(threads)
        .build_global()
        .map_err(|error| format!("failed to configure Rayon: {error}"))?;
    RAYON_THREADS
        .set(threads)
        .map_err(|_| "Rayon thread configuration raced with another request".to_owned())
}

fn run(request: &AdapterRequest) -> Result<RunOutcome, Box<dyn Error>> {
    if request.workload != WORKLOAD {
        return Err(format!(
            "unsupported workload {}; expected {WORKLOAD}",
            request.workload
        )
        .into());
    }
    configure_rayon(request.threads)?;
    let steps = usize::try_from(request.scale)?;

    let native_timer = PhaseTimer::start();
    let (initial, factor, output) = values(request);
    measured_event(
        request,
        "native_execution",
        &native_timer,
        BTreeMap::from([("application_units".to_owned(), request.scale as f64)]),
    )?;

    let witness_timer = PhaseTimer::start();
    let cs = ConstraintSystem::<Fr>::new_ref();
    MultiplicativeChain {
        initial: Some(initial),
        factor: Some(factor),
        output: Some(output),
        steps,
    }
    .generate_constraints(cs.clone())?;
    let constraints = cs.num_constraints();
    if !cs.is_satisfied()? {
        return Err("controlled kernel witness does not satisfy R1CS".into());
    }
    measured_event(
        request,
        "witness",
        &witness_timer,
        BTreeMap::from([("constraint_count".to_owned(), constraints as f64)]),
    )?;

    let setup_timer = PhaseTimer::start();
    let mut setup_rng = StdRng::seed_from_u64(request.seed ^ 0xA11C_E5E7);
    let proving_key = Groth16::<Bn254>::generate_random_parameters_with_reduction(
        MultiplicativeChain {
            initial: None,
            factor: None,
            output: None,
            steps,
        },
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
        MultiplicativeChain {
            initial: Some(initial),
            factor: Some(factor),
            output: Some(output),
            steps,
        },
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

    let mut public_output = output;
    if request.invalid_case.as_deref() == Some("wrong_public_input") {
        public_output += Fr::ONE;
    } else if request.invalid_case.is_some() {
        return Err(format!(
            "unsupported invalid case: {}",
            request.invalid_case.as_deref().unwrap_or_default()
        )
        .into());
    }
    let verify_core_timer = PhaseTimer::start();
    let verify_ok =
        Groth16::<Bn254>::verify_proof(&processed_vk, &decoded_proof, &[factor, public_output])?;
    let verify_core_elapsed = verify_core_timer.elapsed();
    let verify_total_elapsed = verify_total_timer.elapsed();
    measured_duration(
        request,
        "deserialize",
        deserialize_elapsed,
        BTreeMap::from([("proof_bytes".to_owned(), proof_buffer.len() as f64)]),
    )?;
    measured_duration(request, "verify_core", verify_core_elapsed, BTreeMap::new())?;
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
        native_work_units: request.scale,
        public_inputs: 2,
        constraints: u64::try_from(outcome.constraints)?,
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
    fn rejects_mislabeled_workload() {
        let request = AdapterRequest {
            run_id: "wrong-workload".to_owned(),
            workload: "credential".to_owned(),
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
