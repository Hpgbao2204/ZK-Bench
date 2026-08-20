use ark_bls12_381::{Bls12_381, Fr};
use ark_ff::Field;
use ark_groth16::{Groth16, Proof, prepare_verifying_key};
use ark_relations::gr1cs::{
    ConstraintSynthesizer, ConstraintSystem, ConstraintSystemRef, SynthesisError,
};
use ark_serialize::{CanonicalDeserialize, CanonicalSerialize};
use ark_std::rand::{SeedableRng, rngs::StdRng};
use rayon::ThreadPoolBuilder;
use std::collections::BTreeMap;
use std::error::Error;
use std::sync::OnceLock;
use zkbench_adapter_sdk::{
    AdapterRequest, AdapterResult, PhaseEvent, PhaseTimer, SCHEMA_VERSION, emit, emit_result,
    read_request_from_stdin,
};

#[path = "../../ark-groth16/src/relations.rs"]
mod relations;

const ADAPTER: &str = "ark-groth16-0.6.0-bls12-381";
const WORKLOAD: &str = "controlled_kernel";
static RAYON_THREADS: OnceLock<Result<usize, String>> = OnceLock::new();

#[derive(Clone)]
struct MultiplicativeChain {
    initial: Option<Fr>,
    factor: Option<Fr>,
    output: Option<Fr>,
    steps: usize,
}

#[derive(Clone)]
enum BenchmarkCircuit {
    Controlled(MultiplicativeChain),
    Application(relations::ApplicationCircuit),
}

impl ConstraintSynthesizer<Fr> for BenchmarkCircuit {
    fn generate_constraints(self, cs: ConstraintSystemRef<Fr>) -> Result<(), SynthesisError> {
        match self {
            Self::Controlled(circuit) => circuit.generate_constraints(cs),
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
                || ark_relations::lc!() + current_var,
                || ark_relations::lc!() + factor_var,
                || ark_relations::lc!() + next_var,
            )?;
            current_value = next_value;
            current_var = next_var;
        }
        Ok(())
    }
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
        Ok(value) if *value == threads => Ok(()),
        Ok(value) => Err(format!(
            "adapter process already uses {value} Rayon threads; launch one process per setting"
        )),
        Err(error) => Err(error.clone()),
    }
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

fn build_plan(request: &AdapterRequest) -> Result<BenchmarkPlan, Box<dyn Error>> {
    if request.workload == WORKLOAD {
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
        "unsupported workload {}; expected controlled_kernel, credential, batched_state, or private_swap",
        request.workload
    )
    .into())
}

fn unsupported_events(request: &AdapterRequest) -> Result<(), String> {
    let reason = "Arkworks Groth16 does not expose stable per-run FFT/MSM/commitment hooks";
    for phase in ["fft_ntt", "msm", "commitment", "key_load"] {
        emit(&PhaseEvent::unsupported(request, ADAPTER, phase, reason))?;
    }
    Ok(())
}

fn run(request: &AdapterRequest) -> Result<(bool, usize, usize, usize), Box<dyn Error>> {
    configure_rayon(request.threads)?;

    let native_timer = PhaseTimer::start();
    let plan = build_plan(request)?;
    let mut native_metrics = plan.profile.clone();
    native_metrics.insert("native_work_units".to_owned(), plan.native_units as f64);
    if request.workload != WORKLOAD {
        native_metrics.insert(
            "relation_digest".to_owned(),
            relations::relation_digest(request)?,
        );
    }
    emit(&PhaseEvent::measured(
        request,
        ADAPTER,
        "native_execution",
        native_timer.elapsed(),
        native_metrics,
    )?)?;

    let witness_timer = PhaseTimer::start();
    let cs = ConstraintSystem::<Fr>::new_ref();
    plan.circuit.clone().generate_constraints(cs.clone())?;
    let constraints = cs.num_constraints();
    if !cs.is_satisfied()? {
        return Err("controlled witness does not satisfy R1CS".into());
    }
    emit(&PhaseEvent::measured(
        request,
        ADAPTER,
        "witness",
        witness_timer.elapsed(),
        {
            let mut metrics = plan.profile.clone();
            metrics.insert("constraint_count".to_owned(), constraints as f64);
            metrics
        },
    )?)?;

    let setup_timer = PhaseTimer::start();
    let mut setup_rng = StdRng::seed_from_u64(request.seed ^ 0xA11C_E5E7);
    let proving_key = Groth16::<Bls12_381>::generate_random_parameters_with_reduction(
        plan.setup_circuit,
        &mut setup_rng,
    )?;
    let processed_vk = prepare_verifying_key(&proving_key.vk);
    emit(&PhaseEvent::measured(
        request,
        ADAPTER,
        "setup_or_preprocess",
        setup_timer.elapsed(),
        BTreeMap::from([("constraint_count".to_owned(), constraints as f64)]),
    )?)?;

    let prove_timer = PhaseTimer::start();
    let mut proof_rng = StdRng::seed_from_u64(request.seed ^ 0xBADC_0FFE);
    let proof = Groth16::<Bls12_381>::create_random_proof_with_reduction(
        plan.circuit,
        &proving_key,
        &mut proof_rng,
    )?;
    emit(&PhaseEvent::measured(
        request,
        ADAPTER,
        "prove_total",
        prove_timer.elapsed(),
        BTreeMap::from([("constraint_count".to_owned(), constraints as f64)]),
    )?)?;

    let serialize_timer = PhaseTimer::start();
    let mut proof_buffer = Vec::new();
    proof.serialize_compressed(&mut proof_buffer)?;
    emit(&PhaseEvent::measured(
        request,
        ADAPTER,
        "serialize",
        serialize_timer.elapsed(),
        BTreeMap::from([("proof_bytes".to_owned(), proof_buffer.len() as f64)]),
    )?)?;

    let verify_total_timer = PhaseTimer::start();
    let deserialize_timer = PhaseTimer::start();
    let decoded_proof = Proof::<Bls12_381>::deserialize_compressed(proof_buffer.as_slice())?;
    emit(&PhaseEvent::measured(
        request,
        ADAPTER,
        "deserialize",
        deserialize_timer.elapsed(),
        BTreeMap::from([("proof_bytes".to_owned(), proof_buffer.len() as f64)]),
    )?)?;
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
    let verify_ok =
        Groth16::<Bls12_381>::verify_proof(&processed_vk, &decoded_proof, &public_inputs)?;
    let verify_core_elapsed = verify_core_timer.elapsed();
    emit(&PhaseEvent::measured(
        request,
        ADAPTER,
        "verify_core",
        verify_core_elapsed,
        BTreeMap::new(),
    )?)?;
    if request.invalid_case.is_some() {
        emit(&PhaseEvent::measured(
            request,
            ADAPTER,
            "invalid_reject",
            verify_core_elapsed,
            BTreeMap::new(),
        )?)?;
    }
    emit(&PhaseEvent::measured(
        request,
        ADAPTER,
        "verify_total",
        verify_total_timer.elapsed(),
        BTreeMap::new(),
    )?)?;
    unsupported_events(request)?;
    Ok((
        verify_ok,
        proof_buffer.len(),
        constraints,
        public_inputs.len(),
    ))
}

fn main() {
    if let Err(error) = real_main() {
        eprintln!("{ADAPTER}: {error}");
        std::process::exit(1);
    }
}

fn real_main() -> Result<(), Box<dyn Error>> {
    let request = read_request_from_stdin()?;
    let (verify_ok, proof_bytes, constraints, public_inputs) = run(&request)?;
    emit_result(&AdapterResult {
        schema_version: SCHEMA_VERSION,
        event_type: "result",
        run_id: request.run_id.clone(),
        adapter: ADAPTER.to_owned(),
        verify_ok,
        proof_bytes: proof_bytes as u64,
        native_work_units: request.scale,
        public_inputs: public_inputs as u64,
        constraints: constraints as u64,
        relation_unit: "r1cs_constraints".to_owned(),
        invalid_case: request.invalid_case.clone(),
        error_type: if verify_ok {
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

    fn request() -> AdapterRequest {
        AdapterRequest {
            run_id: "bls-valid".to_owned(),
            workload: WORKLOAD.to_owned(),
            scale: 8,
            threads: 2,
            seed: 7,
            mode: "warm".to_owned(),
            invalid_case: None,
            parameters: BTreeMap::new(),
        }
    }

    #[test]
    fn valid_and_invalid_public_inputs() {
        let valid = run(&request()).unwrap();
        assert!(valid.0);
        assert!(valid.1 > 1);
        let mut invalid = request();
        invalid.invalid_case = Some("wrong_public_input".to_owned());
        assert!(!run(&invalid).unwrap().0);
    }

    #[test]
    fn paper_application_uses_bls12_381_and_exact_target_size() {
        let mut value = request();
        value.workload = "credential".to_owned();
        value.scale = 1024;
        value.parameters = BTreeMap::from([
            ("age_bits".to_owned(), 8_u64.into()),
            ("application_units".to_owned(), 2_u64.into()),
            ("hash_rounds".to_owned(), 5_u64.into()),
            ("scale_mode".to_owned(), "target_native_size".into()),
        ]);
        let outcome = run(&value).unwrap();
        assert!(outcome.0);
        assert_eq!(outcome.2, 1024);
    }
}
