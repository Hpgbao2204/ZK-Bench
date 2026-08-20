use rayon::ThreadPoolBuilder;
use std::collections::BTreeMap;
use std::error::Error;
use std::sync::OnceLock;
use winterfell::crypto::{DefaultRandomCoin, MerkleTree, hashers::Blake3_256};
use winterfell::math::{FieldElement, StarkField, ToElements, fields::f128::BaseElement};
use winterfell::{
    Air, AirContext, Assertion, AuxRandElements, BatchingMethod, CompositionPoly,
    CompositionPolyTrace, DefaultConstraintCommitment, DefaultConstraintEvaluator, DefaultTraceLde,
    EvaluationFrame, FieldExtension, PartitionOptions, Proof, ProofOptions, Prover, StarkDomain,
    Trace, TraceInfo, TracePolyTable, TraceTable, TransitionConstraintDegree,
};
use zkbench_adapter_sdk::{
    AdapterRequest, AdapterResult, PhaseEvent, PhaseTimer, SCHEMA_VERSION, emit, emit_result,
    read_request_from_stdin,
};

const ADAPTER: &str = "winterfell-0.13.1-f128";
const WORKLOAD: &str = "controlled_kernel";
const CREDENTIAL_WORKLOAD: &str = "credential";
const STATE_WORKLOAD: &str = "batched_state";
const SWAP_WORKLOAD: &str = "private_swap";
const TRACE_WIDTH: usize = 1;
static RAYON_THREADS: OnceLock<Result<usize, String>> = OnceLock::new();

#[derive(Clone, Copy)]
struct PublicInputs {
    start: BaseElement,
    factor: BaseElement,
    result: BaseElement,
}

impl ToElements<BaseElement> for PublicInputs {
    fn to_elements(&self) -> Vec<BaseElement> {
        vec![self.start, self.factor, self.result]
    }
}

struct WorkAir {
    context: AirContext<BaseElement>,
    start: BaseElement,
    factor: BaseElement,
    result: BaseElement,
}

impl Air for WorkAir {
    type BaseField = BaseElement;
    type PublicInputs = PublicInputs;

    fn new(trace_info: TraceInfo, pub_inputs: PublicInputs, options: ProofOptions) -> Self {
        assert_eq!(trace_info.width(), TRACE_WIDTH);
        Self {
            context: AirContext::new(
                trace_info,
                vec![TransitionConstraintDegree::new(1)],
                2,
                options,
            ),
            start: pub_inputs.start,
            factor: pub_inputs.factor,
            result: pub_inputs.result,
        }
    }

    fn evaluate_transition<E: FieldElement + From<Self::BaseField>>(
        &self,
        frame: &EvaluationFrame<E>,
        _periodic_values: &[E],
        result: &mut [E],
    ) {
        let current = frame.current()[0];
        let factor = E::from(self.factor);
        result[0] = frame.next()[0] - (current * factor);
    }

    fn get_assertions(&self) -> Vec<Assertion<Self::BaseField>> {
        let last = self.trace_length() - 1;
        vec![
            Assertion::single(0, 0, self.start),
            Assertion::single(0, last, self.result),
        ]
    }

    fn context(&self) -> &AirContext<Self::BaseField> {
        &self.context
    }
}

struct WorkProver {
    options: ProofOptions,
    factor: BaseElement,
}

impl WorkProver {
    fn new(options: ProofOptions, factor: BaseElement) -> Self {
        Self { options, factor }
    }
}

impl Prover for WorkProver {
    type BaseField = BaseElement;
    type Air = WorkAir;
    type Trace = TraceTable<Self::BaseField>;
    type HashFn = Blake3_256<Self::BaseField>;
    type VC = MerkleTree<Self::HashFn>;
    type RandomCoin = DefaultRandomCoin<Self::HashFn>;
    type TraceLde<E: FieldElement<BaseField = Self::BaseField>> =
        DefaultTraceLde<E, Self::HashFn, Self::VC>;
    type ConstraintCommitment<E: FieldElement<BaseField = Self::BaseField>> =
        DefaultConstraintCommitment<E, Self::HashFn, Self::VC>;
    type ConstraintEvaluator<'a, E: FieldElement<BaseField = Self::BaseField>> =
        DefaultConstraintEvaluator<'a, Self::Air, E>;

    fn get_pub_inputs(&self, trace: &Self::Trace) -> PublicInputs {
        let last = trace.length() - 1;
        PublicInputs {
            start: trace.get(0, 0),
            factor: self.factor,
            result: trace.get(0, last),
        }
    }

    fn options(&self) -> &ProofOptions {
        &self.options
    }

    fn new_trace_lde<E: FieldElement<BaseField = Self::BaseField>>(
        &self,
        trace_info: &TraceInfo,
        main_trace: &winterfell::matrix::ColMatrix<Self::BaseField>,
        domain: &StarkDomain<Self::BaseField>,
        partition_options: PartitionOptions,
    ) -> (Self::TraceLde<E>, TracePolyTable<E>) {
        DefaultTraceLde::new(trace_info, main_trace, domain, partition_options)
    }

    fn build_constraint_commitment<E: FieldElement<BaseField = Self::BaseField>>(
        &self,
        composition_poly_trace: CompositionPolyTrace<E>,
        num_constraint_composition_columns: usize,
        domain: &StarkDomain<Self::BaseField>,
        partition_options: PartitionOptions,
    ) -> (Self::ConstraintCommitment<E>, CompositionPoly<E>) {
        DefaultConstraintCommitment::new(
            composition_poly_trace,
            num_constraint_composition_columns,
            domain,
            partition_options,
        )
    }

    fn new_evaluator<'a, E: FieldElement<BaseField = Self::BaseField>>(
        &self,
        air: &'a Self::Air,
        aux_rand_elements: Option<AuxRandElements<E>>,
        composition_coefficients: winterfell::ConstraintCompositionCoefficients<E>,
    ) -> Self::ConstraintEvaluator<'a, E> {
        DefaultConstraintEvaluator::new(air, aux_rand_elements, composition_coefficients)
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

fn options() -> ProofOptions {
    ProofOptions::new(
        32,
        8,
        0,
        FieldExtension::None,
        8,
        31,
        BatchingMethod::Linear,
        BatchingMethod::Linear,
    )
}

fn start_value(request: &AdapterRequest) -> BaseElement {
    let domain = match request.workload.as_str() {
        CREDENTIAL_WORKLOAD => 101_u64,
        STATE_WORKLOAD => 211_u64,
        SWAP_WORKLOAD => 307_u64,
        _ => 3_u64,
    };
    BaseElement::new(request.seed.wrapping_add(domain) as u128)
}

fn supports(workload: &str) -> bool {
    matches!(
        workload,
        WORKLOAD | CREDENTIAL_WORKLOAD | STATE_WORKLOAD | SWAP_WORKLOAD
    )
}

fn trace_rows(request: &AdapterRequest) -> Result<usize, String> {
    let scale_mode = request
        .parameters
        .get("scale_mode")
        .map(|value| value.as_str().ok_or_else(|| "scale_mode must be a string".to_owned()))
        .transpose()?
        .unwrap_or("application_units");
    if !matches!(scale_mode, "application_units" | "target_native_size") {
        return Err(format!("unsupported scale_mode: {scale_mode}"));
    }
    let value = if scale_mode == "target_native_size" {
        request.scale
    } else {
        request
            .parameters
            .get("target_native_size")
            .map(|value| {
                value
                    .as_u64()
                    .ok_or_else(|| "target_native_size must be a nonnegative integer".to_owned())
            })
            .transpose()?
            .unwrap_or(request.scale)
    };
    if value <= 1 {
        return Err("AIR trace size must exceed excluded numeric boundaries".to_owned());
    }
    let rows = usize::try_from(value).map_err(|_| "AIR trace size does not fit usize")?;
    if !rows.is_power_of_two() {
        return Err("Winterfell AIR trace size must be a power of two".to_owned());
    }
    Ok(rows)
}

fn factor_value(request: &AdapterRequest) -> BaseElement {
    let mut digest = 14_695_981_039_346_656_037_u64;
    for byte in request.workload.as_bytes() {
        digest ^= u64::from(*byte);
        digest = digest.wrapping_mul(1_099_511_628_211_u64);
    }
    for (name, value) in &request.parameters {
        if name == "target_native_size" {
            continue;
        }
        for byte in name.bytes().chain(value.to_string().bytes()) {
            digest ^= u64::from(byte);
            digest = digest.wrapping_mul(1_099_511_628_211_u64);
        }
    }
    BaseElement::new(request.seed.wrapping_add(digest).max(2) as u128)
}

fn result_value(start: BaseElement, factor: BaseElement, steps: usize) -> BaseElement {
    let mut result = start;
    // A trace with `steps` rows contains `steps - 1` transitions. Keep the
    // public terminal value aligned with the last trace row.
    for _ in 1..steps {
        result *= factor;
    }
    result
}

fn build_trace(start: BaseElement, factor: BaseElement, steps: usize) -> TraceTable<BaseElement> {
    let mut trace = TraceTable::new(TRACE_WIDTH, steps);
    trace.fill(
        |state| {
            state[0] = start;
        },
        |_, state| state[0] *= factor,
    );
    trace
}

fn native_relation_size(trace_rows: usize) -> Result<u64, String> {
    trace_rows
        .checked_mul(TRACE_WIDTH)
        .and_then(|value| u64::try_from(value).ok())
        .ok_or_else(|| "AIR trace-cell count overflow".to_owned())
}

fn measured(
    request: &AdapterRequest,
    phase: &str,
    timer: PhaseTimer,
    metrics: BTreeMap<String, f64>,
) -> Result<(), String> {
    emit(&PhaseEvent::measured(
        request,
        ADAPTER,
        phase,
        timer.elapsed(),
        metrics,
    )?)
}

fn unsupported_events(request: &AdapterRequest) -> Result<(), String> {
    let reason = "Winterfell exposes transparent trace/AIR proofing; no trusted setup or KZG commitment phase";
    for phase in ["setup_or_preprocess", "key_load", "msm", "fft_ntt"] {
        emit(&PhaseEvent::unsupported(request, ADAPTER, phase, reason))?;
    }
    Ok(())
}

fn run(request: &AdapterRequest) -> Result<(Proof, PublicInputs, usize, usize), Box<dyn Error>> {
    if !supports(&request.workload) {
        return Err(format!(
            "unsupported workload {}; expected controlled_kernel, credential, batched_state, or private_swap",
            request.workload
        )
        .into());
    }
    if request.workload == WORKLOAD && !request.parameters.is_empty() {
        return Err("controlled_kernel does not accept workload parameters".into());
    }
    configure_rayon(request.threads)?;
    let steps = trace_rows(request)?;

    let native_timer = PhaseTimer::start();
    let start = start_value(request);
    let factor = factor_value(request);
    let result = result_value(start, factor, steps);
    let application_units = request
        .parameters
        .get("application_units")
        .and_then(|value| value.as_u64())
        .unwrap_or(request.scale);
    let mut native_metrics = BTreeMap::from([
        ("application_units".to_owned(), application_units as f64),
        ("air_trace_cells".to_owned(), native_relation_size(steps)? as f64),
        ("relation_digest".to_owned(), factor.as_int() as f64),
    ]);
    if request.parameters.contains_key("target_native_size") {
        native_metrics.insert("target_native_size".to_owned(), steps as f64);
    }
    measured(
        request,
        "native_execution",
        native_timer,
        native_metrics,
    )?;

    let witness_timer = PhaseTimer::start();
    let trace = build_trace(start, factor, steps);
    let trace_cells = native_relation_size(steps)?;
    measured(
        request,
        "witness",
        witness_timer,
        BTreeMap::from([
            ("trace_rows".to_owned(), steps as f64),
            ("air_trace_cells".to_owned(), trace_cells as f64),
        ]),
    )?;

    unsupported_events(request)?;
    let prove_timer = PhaseTimer::start();
    let prover = WorkProver::new(options(), factor);
    let proof = prover.prove(trace)?;
    measured(
        request,
        "prove_total",
        prove_timer,
        BTreeMap::from([("trace_rows".to_owned(), steps as f64)]),
    )?;

    let serialize_timer = PhaseTimer::start();
    let proof_bytes = proof.to_bytes();
    measured(
        request,
        "serialize",
        serialize_timer,
        BTreeMap::from([("proof_bytes".to_owned(), proof_bytes.len() as f64)]),
    )?;
    let deserialize_timer = PhaseTimer::start();
    let decoded = Proof::from_bytes(&proof_bytes)?;
    measured(
        request,
        "deserialize",
        deserialize_timer,
        BTreeMap::from([("proof_bytes".to_owned(), proof_bytes.len() as f64)]),
    )?;
    Ok((
        decoded,
        PublicInputs {
            start,
            factor,
            result,
        },
        proof_bytes.len(),
        steps,
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
    let (proof, mut public_inputs, proof_bytes, steps) = run(&request)?;
    if request.invalid_case.as_deref() == Some("wrong_public_input") {
        public_inputs.result += BaseElement::ONE;
    } else if request.invalid_case.is_some() {
        return Err(format!(
            "unsupported invalid case: {}",
            request.invalid_case.as_deref().unwrap_or_default()
        )
        .into());
    }
    let verify_total_timer = PhaseTimer::start();
    let verify_ok = winterfell::verify::<
        WorkAir,
        Blake3_256<BaseElement>,
        DefaultRandomCoin<Blake3_256<BaseElement>>,
        MerkleTree<Blake3_256<BaseElement>>,
    >(
        proof,
        public_inputs,
        &winterfell::AcceptableOptions::MinConjecturedSecurity(95),
    )
    .is_ok();
    let verify_elapsed = verify_total_timer.elapsed();
    emit(&PhaseEvent::measured(
        &request,
        ADAPTER,
        "verify_total",
        verify_elapsed,
        BTreeMap::from([("trace_rows".to_owned(), steps as f64)]),
    )?)?;
    if request.invalid_case.is_some() {
        emit(&PhaseEvent::measured(
            &request,
            ADAPTER,
            "invalid_reject",
            verify_elapsed,
            BTreeMap::from([("trace_rows".to_owned(), steps as f64)]),
        )?)?;
    }
    emit_result(&AdapterResult {
        schema_version: SCHEMA_VERSION,
        event_type: "result",
        run_id: request.run_id.clone(),
        adapter: ADAPTER.to_owned(),
        verify_ok,
        proof_bytes: proof_bytes as u64,
        native_work_units: request.scale,
        public_inputs: 3,
        constraints: native_relation_size(steps)?,
        relation_unit: "air_trace_cells".to_owned(),
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

    #[test]
    fn trace_has_expected_relation() {
        let trace = build_trace(BaseElement::new(3), BaseElement::new(5), 16);
        assert_eq!(trace.length(), 16);
        assert_eq!(
            trace.get(0, 1),
            trace.get(0, 0) * BaseElement::new(5)
        );
        assert_eq!(
            result_value(BaseElement::new(3), BaseElement::new(5), 16),
            trace.get(0, trace.length() - 1)
        );
    }

    #[test]
    fn native_relation_size_scales_with_trace_cells() {
        assert_eq!(native_relation_size(16).unwrap(), 16);
        assert_eq!(native_relation_size(1024).unwrap(), 1024);
        assert!(native_relation_size(1024).unwrap() > 1);
    }

    #[test]
    fn paper_workloads_accept_exact_target_trace_size() {
        for workload in [CREDENTIAL_WORKLOAD, STATE_WORKLOAD, SWAP_WORKLOAD] {
            let request = AdapterRequest {
                run_id: format!("winterfell-{workload}"),
                workload: workload.to_owned(),
                scale: 2,
                threads: 1,
                seed: 7,
                mode: "cold".to_owned(),
                invalid_case: None,
                parameters: BTreeMap::from([(
                    "target_native_size".to_owned(),
                    256_u64.into(),
                )]),
            };
            assert_eq!(trace_rows(&request).unwrap(), 256);
            let (_, _, _, rows) = run(&request).unwrap();
            assert_eq!(rows, 256);
        }
    }
}
