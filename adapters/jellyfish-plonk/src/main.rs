use ark_bn254_jf::{Bn254, Fr};
use ark_ff_jf::Field;
use ark_serialize_jf::{CanonicalDeserialize, CanonicalSerialize};
use jf_plonk::{
    proof_system::{
        PlonkKzgSnark, UniversalSNARK, structs::Proof,
    },
    transcript::StandardTranscript,
};
use jf_relation::{Arithmetization, Circuit, PlonkCircuit};
use rand_chacha::ChaCha20Rng;
use rand_core::SeedableRng;
use rayon::ThreadPoolBuilder;
use std::{
    collections::BTreeMap,
    error::Error,
    sync::OnceLock,
    time::Duration,
};
use zkbench_adapter_sdk::{
    AdapterRequest, AdapterResult, PhaseEvent, PhaseTimer, SCHEMA_VERSION,
    emit, emit_result, read_request_from_stdin,
};

mod relations;

const ADAPTER: &str = "jellyfish-turboplonk-0.8.0-bn254-kzg";
const WORKLOAD: &str = "controlled_kernel";
static RAYON_THREADS: OnceLock<Result<usize, String>> = OnceLock::new();

type Snark = PlonkKzgSnark<Bn254>;

struct CircuitBundle {
    circuit: PlonkCircuit<Fr>,
    public_inputs: Vec<Fr>,
    logical_gates: usize,
    domain_rows: usize,
    variables: usize,
    profile: BTreeMap<String, f64>,
}

struct RunOutcome {
    proof_bytes: usize,
    domain_rows: usize,
    public_inputs: usize,
    verify_ok: bool,
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
    let reason = "Jellyfish exposes no stable per-proof hook for this phase";
    for phase in ["fft_ntt", "msm", "commitment", "key_load"] {
        emit(&PhaseEvent::unsupported(
            request, ADAPTER, phase, reason,
        ))?;
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

fn build_controlled_circuit(
    request: &AdapterRequest,
) -> Result<CircuitBundle, Box<dyn Error>> {
    if !request.parameters.is_empty() {
        return Err("controlled_kernel does not accept workload parameters".into());
    }
    let (initial, factor, output) = values(request);
    let mut circuit = PlonkCircuit::<Fr>::new_turbo_plonk();
    let factor_var = circuit.create_public_variable(factor)?;
    let output_var = circuit.create_public_variable(output)?;
    let mut current_value = initial;
    let mut current_var = circuit.create_variable(initial)?;
    for index in 0..request.scale {
        let next_value = current_value * factor;
        let next_var = if index + 1 == request.scale {
            output_var
        } else {
            circuit.create_variable(next_value)?
        };
        circuit.mul_gate(current_var, factor_var, next_var)?;
        current_value = next_value;
        current_var = next_var;
    }
    let public_inputs = vec![factor, output];
    circuit.check_circuit_satisfiability(&public_inputs)?;
    let logical_gates = circuit.num_gates();
    circuit.finalize_for_arithmetization()?;
    let domain_rows = circuit.num_gates();
    let variables = circuit.num_vars();
    Ok(CircuitBundle {
        circuit,
        public_inputs,
        logical_gates,
        domain_rows,
        variables,
        profile: BTreeMap::from([("application_units".to_owned(), request.scale as f64)]),
    })
}

fn build_circuit(request: &AdapterRequest) -> Result<CircuitBundle, Box<dyn Error>> {
    if request.workload == WORKLOAD {
        return build_controlled_circuit(request);
    }
    let application = relations::build_application(request)?;
    Ok(CircuitBundle {
        circuit: application.circuit,
        public_inputs: application.public_inputs,
        logical_gates: application.logical_gates,
        domain_rows: application.domain_rows,
        variables: application.variables,
        profile: application.profile,
    })
}

fn run(request: &AdapterRequest) -> Result<RunOutcome, Box<dyn Error>> {
    configure_rayon(request.threads)?;

    let native_timer = PhaseTimer::start();
    let mut native_metrics = BTreeMap::from([(
        "application_units".to_owned(),
        request.scale as f64,
    )]);
    if request.workload == WORKLOAD {
        std::hint::black_box(values(request));
    } else {
        relations::native_execution(request)?;
        native_metrics.insert(
            "relation_digest".to_owned(),
            relations::relation_digest(request)?,
        );
    }
    measured_event(
        request,
        "native_execution",
        &native_timer,
        native_metrics,
    )?;

    let witness_timer = PhaseTimer::start();
    let controlled = build_circuit(request)?;
    let mut witness_metrics = BTreeMap::from([
        (
            "application_units".to_owned(),
            request.scale as f64,
        ),
        (
            "plonk_logical_gates".to_owned(),
            controlled.logical_gates as f64,
        ),
        (
            "plonk_domain_rows".to_owned(),
            controlled.domain_rows as f64,
        ),
        (
            "plonk_variables".to_owned(),
            controlled.variables as f64,
        ),
    ]);
    witness_metrics.extend(controlled.profile.clone());
    measured_duration(
        request,
        "witness",
        witness_timer.elapsed(),
        witness_metrics,
    )?;

    let setup_timer = PhaseTimer::start();
    let srs_size = controlled.circuit.srs_size()?;
    let mut setup_rng =
        ChaCha20Rng::seed_from_u64(request.seed ^ 0xA11C_E5E7);
    let srs = <Snark as UniversalSNARK<Bn254>>::universal_setup_for_testing(
        srs_size,
        &mut setup_rng,
    )?;
    let (proving_key, verifying_key) =
        Snark::preprocess(&srs, &controlled.circuit)?;
    measured_event(
        request,
        "setup_or_preprocess",
        &setup_timer,
        BTreeMap::from([
            ("plonk_domain_rows".to_owned(), controlled.domain_rows as f64),
            ("srs_size".to_owned(), srs_size as f64),
        ]),
    )?;

    let prove_timer = PhaseTimer::start();
    let mut proof_rng =
        ChaCha20Rng::seed_from_u64(request.seed ^ 0xBADC_0FFE);
    let proof = Snark::prove::<_, _, StandardTranscript>(
        &mut proof_rng,
        &controlled.circuit,
        &proving_key,
        None,
    )?;
    measured_event(
        request,
        "prove_total",
        &prove_timer,
        BTreeMap::from([(
            "plonk_domain_rows".to_owned(),
            controlled.domain_rows as f64,
        )]),
    )?;

    let serialize_timer = PhaseTimer::start();
    let mut proof_buffer = Vec::new();
    proof.serialize_compressed(&mut proof_buffer)?;
    measured_event(
        request,
        "serialize",
        &serialize_timer,
        BTreeMap::from([(
            "proof_bytes".to_owned(),
            proof_buffer.len() as f64,
        )]),
    )?;

    let verify_total_timer = PhaseTimer::start();
    let deserialize_timer = PhaseTimer::start();
    let decoded =
        Proof::<Bn254>::deserialize_compressed(proof_buffer.as_slice())?;
    let deserialize_elapsed = deserialize_timer.elapsed();
    let mut public_inputs = controlled.public_inputs;
    if request.invalid_case.as_deref() == Some("wrong_public_input") {
        let last = public_inputs
            .last_mut()
            .ok_or("controlled relation must expose public inputs")?;
        *last += Fr::ONE;
    } else if request.invalid_case.is_some() {
        return Err(format!(
            "unsupported invalid case: {}",
            request.invalid_case.as_deref().unwrap_or_default()
        )
        .into());
    }
    let verify_core_timer = PhaseTimer::start();
    let verify_ok = Snark::verify::<StandardTranscript>(
        &verifying_key,
        &public_inputs,
        &decoded,
        None,
    )
    .is_ok();
    let verify_core_elapsed = verify_core_timer.elapsed();
    let verify_total_elapsed = verify_total_timer.elapsed();
    measured_duration(
        request,
        "deserialize",
        deserialize_elapsed,
        BTreeMap::from([(
            "proof_bytes".to_owned(),
            proof_buffer.len() as f64,
        )]),
    )?;
    measured_duration(
        request,
        "verify_core",
        verify_core_elapsed,
        BTreeMap::new(),
    )?;
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
        domain_rows: controlled.domain_rows,
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
        native_work_units: request.scale,
        public_inputs: u64::try_from(outcome.public_inputs)?,
        constraints: u64::try_from(outcome.domain_rows)?,
        relation_unit: "plonk_domain_rows".to_owned(),
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

    fn request() -> AdapterRequest {
        AdapterRequest {
            run_id: "plonk-valid".to_owned(),
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
    fn controlled_relation_has_explicit_plonk_sizes() {
        let controlled = build_controlled_circuit(&request()).unwrap();
        assert!(controlled.logical_gates > 1);
        assert!(controlled.domain_rows >= controlled.logical_gates);
        assert_eq!(controlled.public_inputs.len(), 2);
    }

    #[test]
    fn valid_and_invalid_public_inputs() {
        let valid = run(&request()).unwrap();
        assert!(valid.verify_ok);
        assert!(valid.proof_bytes > 1);
        let mut invalid_request = request();
        invalid_request.run_id = "plonk-invalid".to_owned();
        invalid_request.invalid_case =
            Some("wrong_public_input".to_owned());
        assert!(!run(&invalid_request).unwrap().verify_ok);
    }

    #[test]
    fn application_workloads_have_native_plonk_encodings() {
        let mut value = request();
        for workload in ["credential", "batched_state", "private_swap"] {
            value.workload = workload.to_owned();
            let application = build_circuit(&value).unwrap();
            assert!(application.logical_gates > 2);
            assert!(application.domain_rows >= application.logical_gates);
        }
    }

    #[test]
    fn private_swap_proves_and_rejects_wrong_public_input() {
        let mut value = request();
        value.workload = "private_swap".to_owned();
        let valid = run(&value).unwrap();
        assert!(valid.verify_ok);
        value.run_id = "plonk-private-swap-invalid".to_owned();
        value.invalid_case = Some("wrong_public_input".to_owned());
        assert!(!run(&value).unwrap().verify_ok);
    }
}
