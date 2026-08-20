use crate::Fr;
use ark_ff_jf::Field;
use ark_serialize_jf::CanonicalSerialize;
use jf_relation::{BoolVar, Circuit, PlonkCircuit, Variable};
use std::collections::BTreeMap;
use std::error::Error;
use zkbench_adapter_sdk::AdapterRequest;

const CREDENTIAL: &str = "credential";
const BATCHED_STATE: &str = "batched_state";
const PRIVATE_SWAP: &str = "private_swap";

#[derive(Clone, Debug)]
struct RelationParameters {
    age_bits: usize,
    update_bits: usize,
    range_bits: usize,
    time_bits: usize,
    hash_rounds: usize,
    merkle_depth: usize,
    membership_paths: usize,
    target_native_size: Option<usize>,
    ablation: String,
}

impl RelationParameters {
    fn from_request(request: &AdapterRequest) -> Result<Self, String> {
        let scale_mode = request
            .parameters
            .get("scale_mode")
            .map(|value| value.as_str().ok_or_else(|| "scale_mode must be a string".to_owned()))
            .transpose()?
            .unwrap_or("application_units");
        if !matches!(scale_mode, "application_units" | "target_native_size") {
            return Err(format!("unsupported scale_mode: {scale_mode}"));
        }
        let ablation = request
            .parameters
            .get("ablation")
            .map(|value| {
                value
                    .as_str()
                    .ok_or_else(|| "ablation must be a categorical string".to_owned())
            })
            .transpose()?
            .unwrap_or("full")
            .to_owned();
        if !matches!(
            ablation.as_str(),
            "full"
                | "no_membership"
                | "no_range"
                | "no_price"
                | "no_authorization"
        ) {
            return Err(format!("unsupported ablation: {ablation}"));
        }
        Ok(Self {
            age_bits: bit_parameter(request, "age_bits", 8)?,
            update_bits: bit_parameter(request, "update_bits", 16)?,
            range_bits: bit_parameter(request, "range_bits", 32)?,
            time_bits: bit_parameter(request, "time_bits", 16)?,
            hash_rounds: numeric_parameter(request, "hash_rounds", 5)?,
            merkle_depth: numeric_parameter(request, "merkle_depth", 8)?,
            membership_paths: numeric_parameter(request, "membership_paths", 2)?,
            target_native_size: if scale_mode == "target_native_size" {
                Some(
                    usize::try_from(request.scale)
                        .map_err(|_| "target native size does not fit usize".to_owned())?,
                )
            } else {
                optional_numeric_parameter(request, "target_native_size")?
            },
            ablation,
        })
    }

    fn membership_enabled(&self) -> bool {
        self.ablation != "no_membership"
    }

    fn range_enabled(&self) -> bool {
        self.ablation != "no_range"
    }

    fn price_enabled(&self) -> bool {
        self.ablation != "no_price"
    }

    fn authorization_enabled(&self) -> bool {
        self.ablation != "no_authorization"
    }
}

fn optional_numeric_parameter(
    request: &AdapterRequest,
    name: &str,
) -> Result<Option<usize>, String> {
    request
        .parameters
        .get(name)
        .map(|value| {
            let value = value
                .as_u64()
                .ok_or_else(|| format!("{name} must be a nonnegative integer"))?;
            if value <= 1 {
                return Err(format!(
                    "{name} must exceed excluded numeric boundary values"
                ));
            }
            usize::try_from(value).map_err(|_| format!("{name} does not fit usize"))
        })
        .transpose()
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
        return Err(format!(
            "{name} must exceed excluded numeric boundary values"
        ));
    }
    usize::try_from(value).map_err(|_| format!("{name} does not fit usize"))
}

fn bit_parameter(
    request: &AdapterRequest,
    name: &str,
    default: usize,
) -> Result<usize, String> {
    let value = numeric_parameter(request, name, default)?;
    if value > 64 {
        return Err(format!("{name} must not exceed 64 bits"));
    }
    Ok(value)
}

fn application_scale(request: &AdapterRequest) -> Result<usize, Box<dyn Error>> {
    if request.parameters.get("scale_mode").and_then(|value| value.as_str())
        == Some("target_native_size")
    {
        Ok(numeric_parameter(request, "application_units", 2)?)
    } else {
        Ok(usize::try_from(request.scale)?)
    }
}

pub struct BuiltApplication {
    pub circuit: PlonkCircuit<Fr>,
    pub public_inputs: Vec<Fr>,
    pub logical_gates: usize,
    pub domain_rows: usize,
    pub variables: usize,
    pub profile: BTreeMap<String, f64>,
}

fn hash_native(left: Fr, right: Fr, rounds: usize) -> Fr {
    let mut state = left + Fr::from(7_u64) * right + Fr::from(11_u64);
    for round in 0..rounds {
        let square = state.square();
        let fourth = square.square();
        state = fourth * state + Fr::from((round as u64) + 19);
    }
    state
}

fn add_constant(
    circuit: &mut PlonkCircuit<Fr>,
    variable: Variable,
    value: Fr,
) -> Result<Variable, Box<dyn Error>> {
    let constant = circuit.create_constant_variable(value)?;
    Ok(circuit.add(variable, constant)?)
}

fn hash_gadget(
    circuit: &mut PlonkCircuit<Fr>,
    left: Variable,
    right: Variable,
    rounds: usize,
) -> Result<Variable, Box<dyn Error>> {
    let seven = circuit.create_constant_variable(Fr::from(7_u64))?;
    let scaled = circuit.mul(seven, right)?;
    let mut state = circuit.add(left, scaled)?;
    state = add_constant(circuit, state, Fr::from(11_u64))?;
    for round in 0..rounds {
        let square = circuit.mul(state, state)?;
        let fourth = circuit.mul(square, square)?;
        let fifth = circuit.mul(fourth, state)?;
        state = add_constant(circuit, fifth, Fr::from((round as u64) + 19))?;
    }
    Ok(state)
}

fn witness(
    circuit: &mut PlonkCircuit<Fr>,
    value: Fr,
) -> Result<Variable, Box<dyn Error>> {
    Ok(circuit.create_variable(value)?)
}

fn public_input(
    circuit: &mut PlonkCircuit<Fr>,
    value: Fr,
) -> Result<Variable, Box<dyn Error>> {
    Ok(circuit.create_public_variable(value)?)
}

fn bounded_witness(
    circuit: &mut PlonkCircuit<Fr>,
    value: u64,
    width: usize,
) -> Result<Variable, Box<dyn Error>> {
    let variable = witness(circuit, Fr::from(value))?;
    circuit.enforce_in_range(variable, width)?;
    Ok(variable)
}

fn enforce_true(
    circuit: &mut PlonkCircuit<Fr>,
    value: bool,
) -> Result<BoolVar, Box<dyn Error>> {
    let variable = circuit.create_boolean_variable(value)?;
    circuit.enforce_true(variable.into())?;
    Ok(variable)
}

fn credential_values(seed: u64, index: usize) -> (u64, u64, u64) {
    let age = 18 + (seed.wrapping_add(index as u64 * 17) % 83);
    let subject = seed.wrapping_add(index as u64 * 31).wrapping_add(101);
    let nonce = seed.wrapping_add(index as u64 * 43).wrapping_add(211);
    (age, subject, nonce)
}

fn state_delta(seed: u64, index: usize, width: usize) -> u64 {
    let mask = if width >= 63 {
        u64::MAX >> 1
    } else {
        (1_u64 << width) - 1
    };
    2 + seed.wrapping_add(index as u64 * 13) % mask.saturating_sub(2)
}

fn swap_values(seed: u64, index: usize) -> (u64, u64, u64) {
    let amount_a = 100 + 2 * (seed.wrapping_add(index as u64 * 5) % 1_000);
    let amount_b = amount_a * 3 / 2;
    let secret = seed.wrapping_add(index as u64 * 47).wrapping_add(401);
    (amount_a, amount_b, secret)
}

fn credential_public_inputs(
    seed: u64,
    scale: usize,
    parameters: &RelationParameters,
) -> Vec<Fr> {
    let mut aggregate = Fr::from(23_u64);
    for index in 0..scale {
        let (age, subject, nonce) = credential_values(seed, index);
        let identity = hash_native(
            Fr::from(subject),
            Fr::from(nonce),
            parameters.hash_rounds,
        );
        let commitment = hash_native(identity, Fr::from(age), parameters.hash_rounds);
        aggregate = hash_native(aggregate, commitment, parameters.hash_rounds);
    }
    vec![Fr::from(18_u64), aggregate]
}

fn state_public_inputs(
    seed: u64,
    scale: usize,
    parameters: &RelationParameters,
) -> Vec<Fr> {
    let initial = Fr::from(seed.wrapping_add(29));
    let mut state = initial;
    let mut digest = Fr::from(31_u64);
    for index in 0..scale {
        let delta = Fr::from(state_delta(seed, index, parameters.update_bits));
        state += delta;
        let update = hash_native(delta, state, parameters.hash_rounds);
        digest = hash_native(digest, update, parameters.hash_rounds);
    }
    vec![initial, state, digest]
}

fn swap_public_inputs(
    seed: u64,
    scale: usize,
    parameters: &RelationParameters,
) -> Vec<Fr> {
    let price_num = Fr::from(3_u64);
    let price_den = Fr::from(2_u64);
    let current_time = Fr::from(10_000_u64);
    let expiry = Fr::from(10_600_u64);
    let domain = Fr::from(seed.wrapping_add(503));
    let mut hashlock_aggregate = Fr::from(37_u64);
    let mut root_aggregate = Fr::from(41_u64);
    for index in 0..scale {
        let (amount_a, amount_b, secret) = swap_values(seed, index);
        let hashlock = hash_native(
            Fr::from(secret),
            domain,
            parameters.hash_rounds,
        );
        hashlock_aggregate = hash_native(
            hashlock_aggregate,
            hashlock,
            parameters.hash_rounds,
        );
        if parameters.membership_enabled() {
            let leaf = hash_native(
                Fr::from(amount_a),
                Fr::from(amount_b),
                parameters.hash_rounds,
            );
            for path in 0..parameters.membership_paths {
                let mut node = hash_native(
                    leaf,
                    Fr::from((path as u64) + 2),
                    parameters.hash_rounds,
                );
                for level in 0..parameters.merkle_depth {
                    let sibling = Fr::from(
                        seed.wrapping_add((index * 101 + path * 17 + level) as u64)
                            .wrapping_add(607),
                    );
                    let direction =
                        ((seed + index as u64 + path as u64 + level as u64) & 1) == 1;
                    node = if direction {
                        hash_native(sibling, node, parameters.hash_rounds)
                    } else {
                        hash_native(node, sibling, parameters.hash_rounds)
                    };
                }
                root_aggregate = hash_native(
                    root_aggregate,
                    node,
                    parameters.hash_rounds,
                );
            }
        }
    }
    let mut inputs = vec![
        price_num,
        price_den,
        current_time,
        expiry,
        domain,
        hashlock_aggregate,
    ];
    if parameters.membership_enabled() {
        inputs.push(root_aggregate);
    }
    inputs
}

fn credential_profile(
    scale: usize,
    parameters: &RelationParameters,
) -> BTreeMap<String, f64> {
    BTreeMap::from([
        ("application_units".to_owned(), scale as f64),
        (
            "hash_invocations".to_owned(),
            (3 * scale) as f64,
        ),
        (
            "range_bits_total".to_owned(),
            (2 * parameters.age_bits * scale) as f64,
        ),
        ("hash_rounds".to_owned(), parameters.hash_rounds as f64),
    ])
}

fn state_profile(
    scale: usize,
    parameters: &RelationParameters,
) -> BTreeMap<String, f64> {
    BTreeMap::from([
        ("application_units".to_owned(), scale as f64),
        ("hash_invocations".to_owned(), (2 * scale) as f64),
        (
            "range_bits_total".to_owned(),
            (parameters.update_bits * scale) as f64,
        ),
        ("hash_rounds".to_owned(), parameters.hash_rounds as f64),
    ])
}

fn swap_profile(
    scale: usize,
    parameters: &RelationParameters,
) -> BTreeMap<String, f64> {
    let mut profile = BTreeMap::from([
        ("application_units".to_owned(), scale as f64),
        ("hash_rounds".to_owned(), parameters.hash_rounds as f64),
    ]);
    let mut hash_invocations = 2 * scale;
    let mut range_bits = parameters.time_bits * scale;
    if parameters.range_enabled() {
        range_bits += 2 * parameters.range_bits * scale;
    }
    if parameters.membership_enabled() {
        hash_invocations += scale
            * parameters.membership_paths
            * (parameters.merkle_depth + 2);
        profile.insert("merkle_depth".to_owned(), parameters.merkle_depth as f64);
        profile.insert(
            "membership_paths".to_owned(),
            (parameters.membership_paths * scale) as f64,
        );
    }
    profile.insert("hash_invocations".to_owned(), hash_invocations as f64);
    profile.insert("range_bits_total".to_owned(), range_bits as f64);
    profile
}

fn pad_to_target_domain(
    circuit: &mut PlonkCircuit<Fr>,
    target: usize,
) -> Result<(), Box<dyn Error>> {
    if !target.is_power_of_two() {
        return Err("target_native_size for PLONK must be a power of two".into());
    }
    let minimum_logical_gates = target / 2 + 1;
    if circuit.num_gates() > target {
        return Err(format!(
            "application circuit already has {} gates, exceeding target_native_size {target}",
            circuit.num_gates()
        )
        .into());
    }
    let pad = circuit.create_variable(Fr::ONE)?;
    while circuit.num_gates() < minimum_logical_gates {
        circuit.mul_gate(pad, pad, pad)?;
    }
    Ok(())
}

fn synthesize_credential(
    circuit: &mut PlonkCircuit<Fr>,
    seed: u64,
    scale: usize,
    parameters: &RelationParameters,
    public: &[Variable],
) -> Result<(), Box<dyn Error>> {
    let min_age = public[0];
    let expected_aggregate = public[1];
    let mut aggregate = circuit.create_constant_variable(Fr::from(23_u64))?;
    for index in 0..scale {
        let (age_value, subject_value, nonce_value) = credential_values(seed, index);
        let age = bounded_witness(circuit, age_value, parameters.age_bits)?;
        let age_delta = bounded_witness(
            circuit,
            age_value - 18,
            parameters.age_bits,
        )?;
        let age_expected = circuit.add(min_age, age_delta)?;
        circuit.enforce_equal(age, age_expected)?;
        let subject = witness(circuit, Fr::from(subject_value))?;
        let nonce = witness(circuit, Fr::from(nonce_value))?;
        let _authorized = enforce_true(circuit, true)?;
        let identity = hash_gadget(circuit, subject, nonce, parameters.hash_rounds)?;
        let commitment = hash_gadget(circuit, identity, age, parameters.hash_rounds)?;
        aggregate = hash_gadget(circuit, aggregate, commitment, parameters.hash_rounds)?;
    }
    circuit.enforce_equal(aggregate, expected_aggregate)?;
    Ok(())
}

fn synthesize_state(
    circuit: &mut PlonkCircuit<Fr>,
    seed: u64,
    scale: usize,
    parameters: &RelationParameters,
    public: &[Variable],
) -> Result<(), Box<dyn Error>> {
    let mut state = public[0];
    let expected_state = public[1];
    let expected_digest = public[2];
    let mut digest = circuit.create_constant_variable(Fr::from(31_u64))?;
    for index in 0..scale {
        let delta = bounded_witness(
            circuit,
            state_delta(seed, index, parameters.update_bits),
            parameters.update_bits,
        )?;
        state = circuit.add(state, delta)?;
        let update = hash_gadget(circuit, delta, state, parameters.hash_rounds)?;
        digest = hash_gadget(circuit, digest, update, parameters.hash_rounds)?;
    }
    circuit.enforce_equal(state, expected_state)?;
    circuit.enforce_equal(digest, expected_digest)?;
    Ok(())
}

fn synthesize_swap(
    circuit: &mut PlonkCircuit<Fr>,
    seed: u64,
    scale: usize,
    parameters: &RelationParameters,
    public: &[Variable],
) -> Result<(), Box<dyn Error>> {
    let price_num = public[0];
    let price_den = public[1];
    let current_time = public[2];
    let expiry = public[3];
    let domain = public[4];
    let expected_hashlocks = public[5];
    let expected_roots = if parameters.membership_enabled() {
        Some(public[6])
    } else {
        None
    };
    let mut hashlock_aggregate = circuit.create_constant_variable(Fr::from(37_u64))?;
    let mut root_aggregate = circuit.create_constant_variable(Fr::from(41_u64))?;
    for index in 0..scale {
        let (amount_a_value, amount_b_value, secret_value) = swap_values(seed, index);
        let amount_a = if parameters.range_enabled() {
            bounded_witness(circuit, amount_a_value, parameters.range_bits)?
        } else {
            witness(circuit, Fr::from(amount_a_value))?
        };
        let amount_b = if parameters.range_enabled() {
            bounded_witness(circuit, amount_b_value, parameters.range_bits)?
        } else {
            witness(circuit, Fr::from(amount_b_value))?
        };
        if parameters.price_enabled() {
            let left = circuit.mul(amount_a, price_num)?;
            let right = circuit.mul(amount_b, price_den)?;
            circuit.enforce_equal(left, right)?;
        }
        let secret = witness(circuit, Fr::from(secret_value))?;
        let hashlock = hash_gadget(circuit, secret, domain, parameters.hash_rounds)?;
        hashlock_aggregate = hash_gadget(
            circuit,
            hashlock_aggregate,
            hashlock,
            parameters.hash_rounds,
        )?;
        let remaining = bounded_witness(circuit, 600_u64, parameters.time_bits)?;
        let time_sum = circuit.add(current_time, remaining)?;
        circuit.enforce_equal(time_sum, expiry)?;
        if parameters.authorization_enabled() {
            let _authorized = enforce_true(circuit, true)?;
        }
        if parameters.membership_enabled() {
            let leaf = hash_gadget(
                circuit,
                amount_a,
                amount_b,
                parameters.hash_rounds,
            )?;
            for path in 0..parameters.membership_paths {
                let path_tag = circuit.create_constant_variable(Fr::from((path as u64) + 2))?;
                let mut node = hash_gadget(circuit, leaf, path_tag, parameters.hash_rounds)?;
                for level in 0..parameters.merkle_depth {
                    let sibling_value = seed
                        .wrapping_add((index * 101 + path * 17 + level) as u64)
                        .wrapping_add(607);
                    let sibling = witness(circuit, Fr::from(sibling_value))?;
                    let direction_value =
                        ((seed + index as u64 + path as u64 + level as u64) & 1) == 1;
                    let direction = circuit.create_boolean_variable(direction_value)?;
                    let left = circuit.conditional_select(direction, node, sibling)?;
                    let right = circuit.conditional_select(direction, sibling, node)?;
                    node = hash_gadget(circuit, left, right, parameters.hash_rounds)?;
                }
                root_aggregate = hash_gadget(
                    circuit,
                    root_aggregate,
                    node,
                    parameters.hash_rounds,
                )?;
            }
        }
    }
    circuit.enforce_equal(hashlock_aggregate, expected_hashlocks)?;
    if let Some(expected_roots) = expected_roots {
        circuit.enforce_equal(root_aggregate, expected_roots)?;
    }
    Ok(())
}

pub fn supports(workload: &str) -> bool {
    matches!(workload, CREDENTIAL | BATCHED_STATE | PRIVATE_SWAP)
}

fn relation_digest_bytes(
    request: &AdapterRequest,
    public_inputs: &[Fr],
) -> Result<f64, Box<dyn Error>> {
    let mut bytes = request.workload.as_bytes().to_vec();
    for (name, value) in &request.parameters {
        bytes.extend_from_slice(name.as_bytes());
        bytes.extend_from_slice(value.to_string().as_bytes());
    }
    for input in public_inputs {
        input.serialize_compressed(&mut bytes)?;
    }
    let mut digest = 14_695_981_039_346_656_037_u64;
    for byte in bytes {
        digest ^= u64::from(byte);
        digest = digest.wrapping_mul(1_099_511_628_211_u64);
    }
    let safe = (digest & ((1_u64 << 52) - 1)).max(2);
    Ok(safe as f64)
}

pub fn relation_digest(request: &AdapterRequest) -> Result<f64, Box<dyn Error>> {
    if !supports(&request.workload) {
        return Err(format!("unsupported application workload: {}", request.workload).into());
    }
    let scale = application_scale(request)?;
    let parameters = RelationParameters::from_request(request)?;
    let public_inputs = match request.workload.as_str() {
        CREDENTIAL => credential_public_inputs(request.seed, scale, &parameters),
        BATCHED_STATE => state_public_inputs(request.seed, scale, &parameters),
        PRIVATE_SWAP => swap_public_inputs(request.seed, scale, &parameters),
        _ => unreachable!("workload checked by supports"),
    };
    relation_digest_bytes(request, &public_inputs)
}

pub fn native_execution(request: &AdapterRequest) -> Result<(), Box<dyn Error>> {
    if !supports(&request.workload) {
        return Err(format!("unsupported application workload: {}", request.workload).into());
    }
    let scale = application_scale(request)?;
    let parameters = RelationParameters::from_request(request)?;
    let inputs = match request.workload.as_str() {
        CREDENTIAL => credential_public_inputs(request.seed, scale, &parameters),
        BATCHED_STATE => state_public_inputs(request.seed, scale, &parameters),
        PRIVATE_SWAP => swap_public_inputs(request.seed, scale, &parameters),
        _ => unreachable!("workload checked by supports"),
    };
    std::hint::black_box(inputs);
    Ok(())
}

pub fn build_application(request: &AdapterRequest) -> Result<BuiltApplication, Box<dyn Error>> {
    if !supports(&request.workload) {
        return Err(format!("unsupported application workload: {}", request.workload).into());
    }
    let scale = application_scale(request)?;
    let parameters = RelationParameters::from_request(request)?;
    let public_inputs = match request.workload.as_str() {
        CREDENTIAL => credential_public_inputs(request.seed, scale, &parameters),
        BATCHED_STATE => state_public_inputs(request.seed, scale, &parameters),
        PRIVATE_SWAP => swap_public_inputs(request.seed, scale, &parameters),
        _ => unreachable!("workload checked by supports"),
    };
    let mut circuit = PlonkCircuit::<Fr>::new_turbo_plonk();
    let public_variables: Vec<Variable> = public_inputs
        .iter()
        .copied()
        .map(|value| public_input(&mut circuit, value))
        .collect::<Result<_, _>>()?;
    match request.workload.as_str() {
        CREDENTIAL => synthesize_credential(
            &mut circuit,
            request.seed,
            scale,
            &parameters,
            &public_variables,
        )?,
        BATCHED_STATE => synthesize_state(
            &mut circuit,
            request.seed,
            scale,
            &parameters,
            &public_variables,
        )?,
        PRIVATE_SWAP => synthesize_swap(
            &mut circuit,
            request.seed,
            scale,
            &parameters,
            &public_variables,
        )?,
        _ => unreachable!("workload checked by supports"),
    }
    if let Some(target) = parameters.target_native_size {
        pad_to_target_domain(&mut circuit, target)?;
    }
    circuit.check_circuit_satisfiability(&public_inputs)?;
    let logical_gates = circuit.num_gates();
    circuit.finalize_for_arithmetization()?;
    let domain_rows = circuit.num_gates();
    if let Some(target) = parameters.target_native_size {
        if domain_rows != target {
            return Err(format!(
                "PLONK finalized domain has {domain_rows} rows, expected target_native_size {target}"
            )
            .into());
        }
    }
    let variables = circuit.num_vars();
    let mut profile = match request.workload.as_str() {
        CREDENTIAL => credential_profile(scale, &parameters),
        BATCHED_STATE => state_profile(scale, &parameters),
        PRIVATE_SWAP => swap_profile(scale, &parameters),
        _ => unreachable!("workload checked by supports"),
    };
    if let Some(target) = parameters.target_native_size {
        profile.insert("target_native_size".to_owned(), target as f64);
    }
    Ok(BuiltApplication {
        circuit,
        public_inputs,
        logical_gates,
        domain_rows,
        variables,
        profile,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;

    fn request(workload: &str) -> AdapterRequest {
        AdapterRequest {
            run_id: format!("plonk-{workload}"),
            workload: workload.to_owned(),
            scale: 2,
            threads: 2,
            seed: 7,
            mode: "warm".to_owned(),
            invalid_case: None,
            parameters: BTreeMap::from([
                ("hash_rounds".to_owned(), 5_u64.into()),
                ("age_bits".to_owned(), 8_u64.into()),
                ("update_bits".to_owned(), 16_u64.into()),
                ("range_bits".to_owned(), 32_u64.into()),
                ("time_bits".to_owned(), 16_u64.into()),
                ("merkle_depth".to_owned(), 4_u64.into()),
                ("membership_paths".to_owned(), 2_u64.into()),
            ]),
        }
    }

    #[test]
    fn application_relations_are_satisfied_and_report_native_sizes() {
        for workload in [CREDENTIAL, BATCHED_STATE, PRIVATE_SWAP] {
            let plan = build_application(&request(workload)).unwrap();
            assert!(plan.logical_gates > 2, "{workload}");
            assert!(plan.domain_rows >= plan.logical_gates, "{workload}");
            assert!(plan.public_inputs.len() > 1, "{workload}");
            assert!(plan.profile["hash_invocations"] > 1.0, "{workload}");
        }
    }

    #[test]
    fn relation_digest_is_stable_for_shared_fixture() {
        let value = request(CREDENTIAL);
        assert_eq!(relation_digest(&value).unwrap(), relation_digest(&value).unwrap());
        let mut changed = value.clone();
        changed.seed += 2;
        assert_ne!(relation_digest(&value).unwrap(), relation_digest(&changed).unwrap());
    }

    #[test]
    fn swap_ablation_changes_public_shape() {
        let mut value = request(PRIVATE_SWAP);
        value
            .parameters
            .insert("ablation".to_owned(), "no_membership".into());
        let plan = build_application(&value).unwrap();
        assert_eq!(plan.public_inputs.len(), 6);
        assert!(!plan.profile.contains_key("merkle_depth"));
        assert!(!plan.profile.contains_key("membership_paths"));
    }

    #[test]
    fn unsupported_ablation_is_rejected() {
        let mut value = request(PRIVATE_SWAP);
        value
            .parameters
            .insert("ablation".to_owned(), "remove_everything".into());
        assert!(build_application(&value).is_err());
    }

    #[test]
    fn target_native_size_pads_to_exact_domain() {
        let mut value = request(CREDENTIAL);
        value
            .parameters
            .insert("target_native_size".to_owned(), 4096_u64.into());
        let plan = build_application(&value).unwrap();
        assert_eq!(plan.domain_rows, 4096);
        assert!(plan.logical_gates <= plan.domain_rows);
    }
}
