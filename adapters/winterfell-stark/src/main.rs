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
    read_request_from_stdin, write_proof_artifact,
};

const ADAPTER: &str = "winterfell-0.13.1-f128";
const WORKLOAD: &str = "controlled_kernel";
const CREDENTIAL_WORKLOAD: &str = "credential";
const STATE_WORKLOAD: &str = "batched_state";
const SWAP_WORKLOAD: &str = "private_swap";
const PROXY_TRACE_WIDTH: usize = 1;
const STATE_TRACE_WIDTH: usize = 29;
const STATE_COL: usize = 0;
const DIGEST_COL: usize = 1;
const DELTA_COL: usize = 2;
const UPDATE_HASH_START: usize = 3;
const DIGEST_HASH_START: usize = 8;
const RANGE_BITS_START: usize = 13;
const STATE_HASH_ROUNDS: usize = 5;
const STATE_UPDATE_BITS: usize = 16;
const STATE_INITIAL_DIGEST: u64 = 31;
static RAYON_THREADS: OnceLock<Result<usize, String>> = OnceLock::new();

#[derive(Clone, Copy)]
struct PublicInputs {
    start: BaseElement,
    aux: BaseElement,
    result: BaseElement,
}

impl ToElements<BaseElement> for PublicInputs {
    fn to_elements(&self) -> Vec<BaseElement> {
        // State predicates use the same public-input order as the pairing
        // adapters: initial state, final state, aggregate digest.
        vec![self.start, self.result, self.aux]
    }
}

#[derive(Clone, Copy)]
enum AirKind {
    SizeProxy,
    StatePredicate,
}

struct WorkAir {
    context: AirContext<BaseElement>,
    pub_inputs: PublicInputs,
    kind: AirKind,
}

fn fifth_power<E: FieldElement>(value: E) -> E {
    let square = value.square();
    square.square() * value
}

impl Air for WorkAir {
    type BaseField = BaseElement;
    type PublicInputs = PublicInputs;

    fn new(trace_info: TraceInfo, pub_inputs: PublicInputs, options: ProofOptions) -> Self {
        let (kind, degrees, assertions) = match trace_info.width() {
            PROXY_TRACE_WIDTH => (
                AirKind::SizeProxy,
                vec![TransitionConstraintDegree::new(1)],
                2,
            ),
            STATE_TRACE_WIDTH => {
                let mut degrees = vec![TransitionConstraintDegree::new(1); 2];
                degrees.extend((0..STATE_UPDATE_BITS).map(|_| TransitionConstraintDegree::new(2)));
                degrees
                    .extend((0..2 * STATE_HASH_ROUNDS).map(|_| TransitionConstraintDegree::new(5)));
                degrees.push(TransitionConstraintDegree::new(1));
                debug_assert_eq!(degrees.len(), STATE_TRACE_WIDTH);
                (AirKind::StatePredicate, degrees, 4)
            }
            width => panic!("unsupported Winterfell trace width {width}"),
        };
        Self {
            context: AirContext::new(trace_info, degrees, assertions, options),
            pub_inputs,
            kind,
        }
    }

    fn evaluate_transition<E: FieldElement + From<Self::BaseField>>(
        &self,
        frame: &EvaluationFrame<E>,
        _periodic_values: &[E],
        result: &mut [E],
    ) {
        let current = frame.current();
        let next = frame.next();
        match self.kind {
            AirKind::SizeProxy => {
                result[0] = next[0] - current[0] * E::from(self.pub_inputs.aux);
            }
            AirKind::StatePredicate => {
                // 1 state transition + 1 delta reconstruction + 16 booleanity
                // checks + 10 H5 round checks + 1 digest transition = 29.
                result[0] = next[STATE_COL] - current[STATE_COL] - current[DELTA_COL];
                let mut reconstructed_delta = E::ZERO;
                for bit_index in 0..STATE_UPDATE_BITS {
                    let bit = current[RANGE_BITS_START + bit_index];
                    reconstructed_delta += bit * E::from(BaseElement::new(1_u128 << bit_index));
                    result[2 + bit_index] = bit * (bit - E::ONE);
                }
                result[1] = current[DELTA_COL] - reconstructed_delta;

                let seven = E::from(BaseElement::new(7));
                let eleven = E::from(BaseElement::new(11));
                let mut update_state = current[DELTA_COL] + next[STATE_COL] * seven + eleven;
                for round in 0..STATE_HASH_ROUNDS {
                    let expected =
                        fifth_power(update_state) + E::from(BaseElement::new((round as u128) + 19));
                    let column = UPDATE_HASH_START + round;
                    result[2 + STATE_UPDATE_BITS + round] = current[column] - expected;
                    update_state = current[column];
                }

                let mut digest_state =
                    current[DIGEST_COL] + current[UPDATE_HASH_START + 4] * seven + eleven;
                for round in 0..STATE_HASH_ROUNDS {
                    let expected =
                        fifth_power(digest_state) + E::from(BaseElement::new((round as u128) + 19));
                    let column = DIGEST_HASH_START + round;
                    result[2 + STATE_UPDATE_BITS + STATE_HASH_ROUNDS + round] =
                        current[column] - expected;
                    digest_state = current[column];
                }
                result[STATE_TRACE_WIDTH - 1] = next[DIGEST_COL] - current[DIGEST_HASH_START + 4];
            }
        }
    }

    fn get_assertions(&self) -> Vec<Assertion<Self::BaseField>> {
        let last = self.trace_length() - 1;
        match self.kind {
            AirKind::SizeProxy => vec![
                Assertion::single(0, 0, self.pub_inputs.start),
                Assertion::single(0, last, self.pub_inputs.result),
            ],
            AirKind::StatePredicate => vec![
                Assertion::single(STATE_COL, 0, self.pub_inputs.start),
                Assertion::single(
                    DIGEST_COL,
                    0,
                    BaseElement::new(STATE_INITIAL_DIGEST as u128),
                ),
                Assertion::single(STATE_COL, last, self.pub_inputs.result),
                Assertion::single(DIGEST_COL, last, self.pub_inputs.aux),
            ],
        }
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
        if trace.width() == STATE_TRACE_WIDTH {
            PublicInputs {
                start: trace.get(STATE_COL, 0),
                aux: trace.get(DIGEST_COL, last),
                result: trace.get(STATE_COL, last),
            }
        } else {
            PublicInputs {
                start: trace.get(0, 0),
                aux: self.factor,
                result: trace.get(0, last),
            }
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

fn scale_mode(request: &AdapterRequest) -> Result<&str, String> {
    let mode = request
        .parameters
        .get("scale_mode")
        .map(|value| {
            value
                .as_str()
                .ok_or_else(|| "scale_mode must be a string".to_owned())
        })
        .transpose()?;
    Ok(mode.unwrap_or("application_units"))
}

fn state_predicate_enabled(request: &AdapterRequest) -> Result<bool, String> {
    if request.workload != STATE_WORKLOAD {
        return Ok(false);
    }
    let default = if scale_mode(request)? == "application_units"
        && !request.parameters.contains_key("target_native_size")
    {
        "application_predicate"
    } else {
        "size_proxy"
    };
    let mode = request
        .parameters
        .get("stark_relation_mode")
        .map(|value| {
            value
                .as_str()
                .ok_or_else(|| "stark_relation_mode must be a string".to_owned())
        })
        .transpose()?
        .unwrap_or(default);
    match mode {
        "application_predicate" => Ok(true),
        "size_proxy" => Ok(false),
        _ => Err(format!("unsupported stark_relation_mode: {mode}")),
    }
}

fn proxy_trace_rows(request: &AdapterRequest) -> Result<usize, String> {
    let scale_mode = scale_mode(request)?;
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

fn state_trace_rows(request: &AdapterRequest) -> Result<usize, String> {
    if scale_mode(request)? != "application_units" {
        return Err(
            "the real state AIR requires scale_mode=application_units; use size_proxy only for archived native-size comparisons"
                .to_owned(),
        );
    }
    let hash_rounds = request
        .parameters
        .get("hash_rounds")
        .and_then(|value| value.as_u64())
        .unwrap_or(STATE_HASH_ROUNDS as u64);
    if hash_rounds != STATE_HASH_ROUNDS as u64 {
        return Err(format!(
            "the state AIR fixes hash_rounds at {STATE_HASH_ROUNDS}, got {hash_rounds}"
        ));
    }
    let update_bits = request
        .parameters
        .get("update_bits")
        .and_then(|value| value.as_u64())
        .unwrap_or(STATE_UPDATE_BITS as u64);
    if update_bits != STATE_UPDATE_BITS as u64 {
        return Err(format!(
            "the state AIR fixes update_bits at {STATE_UPDATE_BITS}, got {update_bits}"
        ));
    }
    let delta_schedule = request
        .parameters
        .get("delta_schedule")
        .map(|value| {
            value
                .as_str()
                .ok_or_else(|| "delta_schedule must be a categorical string".to_owned())
        })
        .transpose()?
        .unwrap_or("splitmix64-v1");
    if delta_schedule != "splitmix64-v1" {
        return Err(format!(
            "the state AIR requires delta_schedule=splitmix64-v1, got {delta_schedule}"
        ));
    }
    let rows = request
        .scale
        .checked_add(1)
        .ok_or_else(|| "state AIR trace length overflow".to_owned())?;
    let rows = usize::try_from(rows).map_err(|_| "state AIR trace length does not fit usize")?;
    if !rows.is_power_of_two() {
        return Err(
            "state application_units plus the initial row must be a power of two (use 127, 255, ..., 8191)"
                .to_owned(),
        );
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

fn build_proxy_trace(
    start: BaseElement,
    factor: BaseElement,
    steps: usize,
) -> TraceTable<BaseElement> {
    let mut trace = TraceTable::new(PROXY_TRACE_WIDTH, steps);
    trace.fill(
        |state| {
            state[0] = start;
        },
        |_, state| state[0] *= factor,
    );
    trace
}

fn splitmix64(mut value: u64) -> u64 {
    value = value.wrapping_add(0x9E37_79B9_7F4A_7C15);
    value = (value ^ (value >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
    value = (value ^ (value >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
    value ^ (value >> 31)
}

fn state_delta(seed: u64, index: usize) -> u64 {
    let mask = (1_u64 << STATE_UPDATE_BITS) - 1;
    let value = splitmix64(seed ^ (index as u64).wrapping_mul(0xD6E8_FEB8_6659_FD93));
    2 + value % (mask - 2)
}

fn h5_round_values(left: BaseElement, right: BaseElement) -> [BaseElement; STATE_HASH_ROUNDS] {
    let mut values = [BaseElement::ZERO; STATE_HASH_ROUNDS];
    let mut state = left + BaseElement::new(7) * right + BaseElement::new(11);
    for (round, value) in values.iter_mut().enumerate() {
        state = state.exp(5_u64.into()) + BaseElement::new((round as u128) + 19);
        *value = state;
    }
    values
}

fn populate_state_row(
    row: &mut [BaseElement],
    seed: u64,
    update_index: usize,
    state_value: BaseElement,
    digest_value: BaseElement,
) {
    let delta_u64 = state_delta(seed, update_index);
    let delta = BaseElement::new(delta_u64 as u128);
    let next_state = state_value + delta;
    let update_hash = h5_round_values(delta, next_state);
    let digest_hash = h5_round_values(digest_value, update_hash[STATE_HASH_ROUNDS - 1]);

    row[STATE_COL] = state_value;
    row[DIGEST_COL] = digest_value;
    row[DELTA_COL] = delta;
    row[UPDATE_HASH_START..UPDATE_HASH_START + STATE_HASH_ROUNDS].copy_from_slice(&update_hash);
    row[DIGEST_HASH_START..DIGEST_HASH_START + STATE_HASH_ROUNDS].copy_from_slice(&digest_hash);
    for bit_index in 0..STATE_UPDATE_BITS {
        row[RANGE_BITS_START + bit_index] =
            BaseElement::new(((delta_u64 >> bit_index) & 1) as u128);
    }
}

fn build_state_trace(request: &AdapterRequest, steps: usize) -> TraceTable<BaseElement> {
    let mut trace = TraceTable::new(STATE_TRACE_WIDTH, steps);
    let seed = request.seed;
    let initial_state = BaseElement::new(seed.wrapping_add(29) as u128);
    let initial_digest = BaseElement::new(STATE_INITIAL_DIGEST as u128);
    trace.fill(
        |row| populate_state_row(row, seed, 0, initial_state, initial_digest),
        |last_updated_row, row| {
            let next_state = row[STATE_COL] + row[DELTA_COL];
            let next_digest = row[DIGEST_HASH_START + STATE_HASH_ROUNDS - 1];
            populate_state_row(row, seed, last_updated_row + 1, next_state, next_digest);
        },
    );
    trace
}

fn native_relation_size(trace_rows: usize, trace_width: usize) -> Result<u64, String> {
    trace_rows
        .checked_mul(trace_width)
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

fn state_public_inputs(request: &AdapterRequest) -> PublicInputs {
    let start = BaseElement::new(request.seed.wrapping_add(29) as u128);
    let mut state = start;
    let mut digest = BaseElement::new(STATE_INITIAL_DIGEST as u128);
    for index in 0..request.scale as usize {
        let delta = BaseElement::new(state_delta(request.seed, index) as u128);
        state += delta;
        let update = h5_round_values(delta, state)[STATE_HASH_ROUNDS - 1];
        digest = h5_round_values(digest, update)[STATE_HASH_ROUNDS - 1];
    }
    PublicInputs {
        start,
        aux: digest,
        result: state,
    }
}

fn safe_digest_metric(value: BaseElement) -> f64 {
    let value = (value.as_int() as u64 & ((1_u64 << 52) - 1)).max(2);
    value as f64
}

fn run(
    request: &AdapterRequest,
) -> Result<(Proof, PublicInputs, usize, usize, usize), Box<dyn Error>> {
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
    let state_predicate = state_predicate_enabled(request)?;
    let steps = if state_predicate {
        state_trace_rows(request)?
    } else {
        proxy_trace_rows(request)?
    };
    let trace_width = if state_predicate {
        STATE_TRACE_WIDTH
    } else {
        PROXY_TRACE_WIDTH
    };
    let trace_cells = native_relation_size(steps, trace_width)?;

    let native_timer = PhaseTimer::start();
    let (public_inputs, factor, relation_digest) = if state_predicate {
        let inputs = state_public_inputs(request);
        (inputs, BaseElement::new(2), safe_digest_metric(inputs.aux))
    } else {
        let start = start_value(request);
        let factor = factor_value(request);
        let result = result_value(start, factor, steps);
        (
            PublicInputs {
                start,
                aux: factor,
                result,
            },
            factor,
            safe_digest_metric(factor),
        )
    };
    let application_units = if state_predicate {
        request.scale
    } else {
        request
            .parameters
            .get("application_units")
            .and_then(|value| value.as_u64())
            .unwrap_or(request.scale)
    };
    let mut native_metrics = BTreeMap::from([
        ("application_units".to_owned(), application_units as f64),
        ("air_trace_cells".to_owned(), trace_cells as f64),
        ("relation_digest".to_owned(), relation_digest),
    ]);
    if state_predicate {
        native_metrics.insert("air_trace_width".to_owned(), trace_width as f64);
        native_metrics.insert(
            "air_transition_constraints".to_owned(),
            STATE_TRACE_WIDTH as f64,
        );
        native_metrics.insert("hash_rounds".to_owned(), STATE_HASH_ROUNDS as f64);
        native_metrics.insert("range_bits".to_owned(), STATE_UPDATE_BITS as f64);
    }
    if request.parameters.contains_key("target_native_size") {
        native_metrics.insert("target_native_size".to_owned(), steps as f64);
    }
    measured(request, "native_execution", native_timer, native_metrics)?;

    let witness_timer = PhaseTimer::start();
    let trace = if state_predicate {
        build_state_trace(request, steps)
    } else {
        build_proxy_trace(public_inputs.start, factor, steps)
    };
    let mut witness_metrics = BTreeMap::from([
        ("trace_rows".to_owned(), steps as f64),
        ("air_trace_cells".to_owned(), trace_cells as f64),
    ]);
    if state_predicate {
        witness_metrics.insert("air_trace_width".to_owned(), trace_width as f64);
    }
    measured(request, "witness", witness_timer, witness_metrics)?;

    unsupported_events(request)?;
    let prove_timer = PhaseTimer::start();
    let prover = WorkProver::new(options(), factor);
    let proof = prover.prove(trace)?;
    measured(
        request,
        "prove_total",
        prove_timer,
        BTreeMap::from([
            ("trace_rows".to_owned(), steps as f64),
            ("air_trace_cells".to_owned(), trace_cells as f64),
        ]),
    )?;

    let serialize_timer = PhaseTimer::start();
    let proof_bytes = proof.to_bytes();
    measured(
        request,
        "serialize",
        serialize_timer,
        BTreeMap::from([("proof_bytes".to_owned(), proof_bytes.len() as f64)]),
    )?;
    write_proof_artifact(request, &proof_bytes)?;
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
        public_inputs,
        proof_bytes.len(),
        steps,
        trace_width,
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
    let (proof, mut public_inputs, proof_bytes, steps, trace_width) = run(&request)?;
    if request.invalid_case.as_deref() == Some("wrong_public_input") {
        if state_predicate_enabled(&request)? {
            public_inputs.aux += BaseElement::ONE;
        } else {
            public_inputs.result += BaseElement::ONE;
        }
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
        native_work_units: native_relation_size(steps, trace_width)?,
        public_inputs: 3,
        constraints: native_relation_size(steps, trace_width)?,
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
    fn splitmix_state_fixture_matches_cross_adapter_vector() {
        let values: Vec<u64> = (0..5).map(|index| state_delta(7, index)).collect();
        assert_eq!(values, [37141, 33678, 29210, 47224, 62443]);
    }

    #[test]
    fn trace_has_expected_relation() {
        let trace = build_proxy_trace(BaseElement::new(3), BaseElement::new(5), 16);
        assert_eq!(trace.length(), 16);
        assert_eq!(trace.get(0, 1), trace.get(0, 0) * BaseElement::new(5));
        assert_eq!(
            result_value(BaseElement::new(3), BaseElement::new(5), 16),
            trace.get(0, trace.length() - 1)
        );
    }

    #[test]
    fn native_relation_size_scales_with_trace_cells() {
        assert_eq!(native_relation_size(16, PROXY_TRACE_WIDTH).unwrap(), 16);
        assert_eq!(native_relation_size(8, STATE_TRACE_WIDTH).unwrap(), 232);
        assert!(native_relation_size(1024, PROXY_TRACE_WIDTH).unwrap() > 1);
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
                parameters: BTreeMap::from([("target_native_size".to_owned(), 256_u64.into())]),
            };
            assert_eq!(proxy_trace_rows(&request).unwrap(), 256);
            let (_, _, _, rows, width) = run(&request).unwrap();
            assert_eq!(rows, 256);
            assert_eq!(width, PROXY_TRACE_WIDTH);
        }
    }

    fn state_request() -> AdapterRequest {
        AdapterRequest {
            run_id: "winterfell-state-predicate".to_owned(),
            workload: STATE_WORKLOAD.to_owned(),
            scale: 127,
            threads: 1,
            seed: 7,
            mode: "cold".to_owned(),
            invalid_case: None,
            parameters: BTreeMap::from([
                ("delta_schedule".to_owned(), "splitmix64-v1".into()),
                ("hash_rounds".to_owned(), (STATE_HASH_ROUNDS as u64).into()),
                ("scale_mode".to_owned(), "application_units".into()),
                (
                    "stark_relation_mode".to_owned(),
                    "application_predicate".into(),
                ),
                ("update_bits".to_owned(), (STATE_UPDATE_BITS as u64).into()),
            ]),
        }
    }

    #[test]
    fn state_trace_encodes_real_updates_and_h5_chains() {
        let request = state_request();
        let rows = state_trace_rows(&request).unwrap();
        assert_eq!(rows, 128);
        let trace = build_state_trace(&request, rows);
        assert_eq!(trace.width(), STATE_TRACE_WIDTH);
        assert_eq!(trace.get(STATE_COL, 0), BaseElement::new(36));
        assert_eq!(
            trace.get(STATE_COL, 1),
            trace.get(STATE_COL, 0) + trace.get(DELTA_COL, 0)
        );
        let expected = state_public_inputs(&request);
        assert_eq!(trace.get(STATE_COL, rows - 1), expected.result);
        assert_eq!(trace.get(DIGEST_COL, rows - 1), expected.aux);
    }

    #[test]
    fn state_air_proves_and_rejects_wrong_public_input() {
        let request = state_request();
        let (proof, public_inputs, _, rows, width) = run(&request).unwrap();
        assert_eq!(width, STATE_TRACE_WIDTH);
        assert!(
            winterfell::verify::<
                WorkAir,
                Blake3_256<BaseElement>,
                DefaultRandomCoin<Blake3_256<BaseElement>>,
                MerkleTree<Blake3_256<BaseElement>>,
            >(
                proof,
                public_inputs,
                &winterfell::AcceptableOptions::MinConjecturedSecurity(95),
            )
            .is_ok()
        );

        let (proof, mut public_inputs, _, _, _) = run(&request).unwrap();
        public_inputs.result += BaseElement::ONE;
        assert!(
            winterfell::verify::<
                WorkAir,
                Blake3_256<BaseElement>,
                DefaultRandomCoin<Blake3_256<BaseElement>>,
                MerkleTree<Blake3_256<BaseElement>>,
            >(
                proof,
                public_inputs,
                &winterfell::AcceptableOptions::MinConjecturedSecurity(95),
            )
            .is_err()
        );
        assert_eq!(native_relation_size(rows, width).unwrap(), 3712);
    }
}
