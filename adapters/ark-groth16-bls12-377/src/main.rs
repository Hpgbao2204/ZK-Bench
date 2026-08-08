use ark_bls12_377::{Bls12_377, Fr};
use ark_ff::Field;
use ark_groth16::{Groth16, Proof, prepare_verifying_key};
use ark_relations::r1cs::{
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

const ADAPTER: &str = "ark-groth16-0.5.0-bls12-377";
const WORKLOAD: &str = "controlled_kernel";
static RAYON_THREADS: OnceLock<Result<usize, String>> = OnceLock::new();

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
            cs.enforce_constraint(
                ark_relations::lc!() + current_var,
                ark_relations::lc!() + factor_var,
                ark_relations::lc!() + next_var,
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
            "adapter process already uses {value} Rayon threads"
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

fn unsupported_events(request: &AdapterRequest) -> Result<(), String> {
    let reason = "Arkworks Groth16 does not expose stable per-run FFT/MSM/commitment hooks";
    for phase in ["fft_ntt", "msm", "commitment", "key_load"] {
        emit(&PhaseEvent::unsupported(request, ADAPTER, phase, reason))?;
    }
    Ok(())
}

fn run(request: &AdapterRequest) -> Result<(bool, usize, usize, usize), Box<dyn Error>> {
    if request.workload != WORKLOAD {
        return Err(format!(
            "unsupported workload {}; expected {WORKLOAD}",
            request.workload
        )
        .into());
    }
    if !request.parameters.is_empty() {
        return Err("controlled_kernel does not accept workload parameters".into());
    }
    configure_rayon(request.threads)?;
    let steps = usize::try_from(request.scale)?;
    let native_timer = PhaseTimer::start();
    let (initial, factor, output) = values(request);
    std::hint::black_box(&output);
    let native_work_units = request.scale;
    emit(&PhaseEvent::measured(
        request,
        ADAPTER,
        "native_execution",
        native_timer.elapsed(),
        BTreeMap::from([("native_work_units".to_owned(), native_work_units as f64)]),
    )?)?;
    let circuit = MultiplicativeChain {
        initial: Some(initial),
        factor: Some(factor),
        output: Some(output),
        steps,
    };
    let witness_timer = PhaseTimer::start();
    let cs = ConstraintSystem::<Fr>::new_ref();
    circuit.clone().generate_constraints(cs.clone())?;
    let constraints = cs.num_constraints();
    if !cs.is_satisfied()? {
        return Err("controlled witness does not satisfy R1CS".into());
    }
    emit(&PhaseEvent::measured(
        request,
        ADAPTER,
        "witness",
        witness_timer.elapsed(),
        BTreeMap::from([("constraint_count".to_owned(), constraints as f64)]),
    )?)?;

    let setup_timer = PhaseTimer::start();
    let mut setup_rng = StdRng::seed_from_u64(request.seed ^ 0xA11C_E5E7);
    let proving_key = Groth16::<Bls12_377>::generate_random_parameters_with_reduction(
        MultiplicativeChain {
            initial: None,
            factor: None,
            output: None,
            steps,
        },
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
    let proof = Groth16::<Bls12_377>::create_random_proof_with_reduction(
        circuit,
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
    let decoded_proof = Proof::<Bls12_377>::deserialize_compressed(proof_buffer.as_slice())?;
    emit(&PhaseEvent::measured(
        request,
        ADAPTER,
        "deserialize",
        deserialize_timer.elapsed(),
        BTreeMap::from([("proof_bytes".to_owned(), proof_buffer.len() as f64)]),
    )?)?;
    let mut public_inputs = vec![factor, output];
    if request.invalid_case.as_deref() == Some("wrong_public_input") {
        public_inputs[1] += Fr::ONE;
    } else if request.invalid_case.is_some() {
        return Err(format!(
            "unsupported invalid case: {}",
            request.invalid_case.as_deref().unwrap_or_default()
        )
        .into());
    }
    let verify_core_timer = PhaseTimer::start();
    let verify_ok =
        Groth16::<Bls12_377>::verify_proof(&processed_vk, &decoded_proof, &public_inputs)?;
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
        run_id: request.run_id,
        adapter: ADAPTER.to_owned(),
        verify_ok,
        proof_bytes: proof_bytes as u64,
        native_work_units: request.scale,
        public_inputs: public_inputs as u64,
        constraints: constraints as u64,
        relation_unit: "r1cs_constraints".to_owned(),
        invalid_case: request.invalid_case,
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
            schema_version: SCHEMA_VERSION,
            event_type: "request",
            run_id: "bls377-test".to_owned(),
            adapter: ADAPTER.to_owned(),
            workload: WORKLOAD.to_owned(),
            scale: 8,
            threads: 1,
            seed: 20260808,
            invalid_case: None,
            parameters: BTreeMap::new(),
        }
    }

    #[test]
    fn valid_and_invalid_public_inputs() {
        let valid = run(&request()).unwrap();
        assert!(valid.0);
        let mut invalid = request();
        invalid.invalid_case = Some("wrong_public_input".to_owned());
        assert!(!run(&invalid).unwrap().0);
    }
}
