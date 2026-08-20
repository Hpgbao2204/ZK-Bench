use crate::Fr;
use ark_ff::Field;
use ark_r1cs_std::{fields::fp::FpVar, prelude::*};
use ark_relations::{
    gr1cs::{ConstraintSynthesizer, ConstraintSystemRef, SynthesisError, Variable},
    lc,
};
use ark_serialize::CanonicalSerialize;
use std::collections::BTreeMap;
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

#[derive(Clone)]
pub struct ApplicationCircuit {
    workload: String,
    scale: usize,
    parameters: RelationParameters,
    seed: Option<u64>,
}

pub struct ApplicationPlan {
    pub circuit: ApplicationCircuit,
    pub setup_circuit: ApplicationCircuit,
    pub public_inputs: Vec<Fr>,
    pub native_units: u64,
    pub profile: BTreeMap<String, f64>,
}

pub fn supports(workload: &str) -> bool {
    matches!(workload, CREDENTIAL | BATCHED_STATE | PRIVATE_SWAP)
}

pub fn build_plan(request: &AdapterRequest) -> Result<ApplicationPlan, String> {
    if !supports(&request.workload) {
        return Err(format!("unsupported application workload: {}", request.workload));
    }
    let scale = if request.parameters.get("scale_mode").and_then(|value| value.as_str())
        == Some("target_native_size")
    {
        numeric_parameter(request, "application_units", 2)?
    } else {
        usize::try_from(request.scale)
            .map_err(|_| "application scale does not fit usize".to_owned())?
    };
    let parameters = RelationParameters::from_request(request)?;
    let circuit = ApplicationCircuit {
        workload: request.workload.clone(),
        scale,
        parameters: parameters.clone(),
        seed: Some(request.seed),
    };
    let public_inputs = circuit.public_inputs(request.seed);
    let profile = circuit.profile();
    Ok(ApplicationPlan {
        setup_circuit: ApplicationCircuit {
            seed: None,
            ..circuit.clone()
        },
        circuit,
        public_inputs,
        native_units: parameters
            .target_native_size
            .map(|value| value as u64)
            .unwrap_or(request.scale),
        profile,
    })
}

fn relation_digest_bytes(
    request: &AdapterRequest,
    public_inputs: &[Fr],
) -> Result<f64, String> {
    let mut bytes = request.workload.as_bytes().to_vec();
    for (name, value) in &request.parameters {
        bytes.extend_from_slice(name.as_bytes());
        bytes.extend_from_slice(value.to_string().as_bytes());
    }
    for input in public_inputs {
        input
            .serialize_compressed(&mut bytes)
            .map_err(|error| format!("failed to serialize relation digest: {error}"))?;
    }
    let mut digest = 14_695_981_039_346_656_037_u64;
    for byte in bytes {
        digest ^= u64::from(byte);
        digest = digest.wrapping_mul(1_099_511_628_211_u64);
    }
    let safe = (digest & ((1_u64 << 52) - 1)).max(2);
    Ok(safe as f64)
}

pub fn relation_digest(request: &AdapterRequest) -> Result<f64, String> {
    let plan = build_plan(request)?;
    relation_digest_bytes(request, &plan.public_inputs)
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

fn hash_gadget(
    left: &FpVar<Fr>,
    right: &FpVar<Fr>,
    rounds: usize,
) -> Result<FpVar<Fr>, SynthesisError> {
    let mut state = left + right * Fr::from(7_u64) + Fr::from(11_u64);
    for round in 0..rounds {
        let square = &state * &state;
        let fourth = &square * &square;
        state = &fourth * &state + Fr::from((round as u64) + 19);
    }
    Ok(state)
}

fn witness_field(
    cs: ConstraintSystemRef<Fr>,
    value: Option<u64>,
) -> Result<FpVar<Fr>, SynthesisError> {
    FpVar::new_witness(cs, || {
        value
            .map(Fr::from)
            .ok_or(SynthesisError::AssignmentMissing)
    })
}

fn input_field(
    cs: ConstraintSystemRef<Fr>,
    value: Option<Fr>,
) -> Result<FpVar<Fr>, SynthesisError> {
    FpVar::new_input(cs, || value.ok_or(SynthesisError::AssignmentMissing))
}

fn bounded_witness(
    cs: ConstraintSystemRef<Fr>,
    value: Option<u64>,
    width: usize,
) -> Result<FpVar<Fr>, SynthesisError> {
    let field = witness_field(cs.clone(), value)?;
    let mut bits = Vec::with_capacity(width);
    for bit_index in 0..width {
        let bit = value.map(|number| ((number >> bit_index) & 1) == 1);
        bits.push(Boolean::new_witness(cs.clone(), || {
            bit.ok_or(SynthesisError::AssignmentMissing)
        })?);
    }
    Boolean::le_bits_to_fp(&bits)?.enforce_equal(&field)?;
    Ok(field)
}

impl ApplicationCircuit {
    fn profile(&self) -> BTreeMap<String, f64> {
        let scale = self.scale as f64;
        let mut profile = BTreeMap::from([
            ("application_units".to_owned(), scale),
            ("hash_rounds".to_owned(), self.parameters.hash_rounds as f64),
        ]);
        if let Some(target) = self.parameters.target_native_size {
            profile.insert("target_native_size".to_owned(), target as f64);
        }
        match self.workload.as_str() {
            CREDENTIAL => {
                profile.insert("hash_invocations".to_owned(), 3.0 * scale);
                profile.insert(
                    "range_bits_total".to_owned(),
                    (2 * self.parameters.age_bits * self.scale) as f64,
                );
            }
            BATCHED_STATE => {
                profile.insert("hash_invocations".to_owned(), 2.0 * scale);
                profile.insert(
                    "range_bits_total".to_owned(),
                    (self.parameters.update_bits * self.scale) as f64,
                );
            }
            PRIVATE_SWAP => {
                let mut hash_invocations = 2 * self.scale;
                if self.parameters.membership_enabled() {
                    hash_invocations += self.scale
                        * self.parameters.membership_paths
                        * (self.parameters.merkle_depth + 2);
                    profile.insert(
                        "merkle_depth".to_owned(),
                        self.parameters.merkle_depth as f64,
                    );
                    profile.insert(
                        "membership_paths".to_owned(),
                        (self.parameters.membership_paths * self.scale) as f64,
                    );
                }
                profile.insert("hash_invocations".to_owned(), hash_invocations as f64);
                let mut range_bits = self.parameters.time_bits * self.scale;
                if self.parameters.range_enabled() {
                    range_bits += 2 * self.parameters.range_bits * self.scale;
                }
                profile.insert("range_bits_total".to_owned(), range_bits as f64);
            }
            _ => unreachable!("workload checked by build_plan"),
        }
        profile
    }

    fn public_inputs(&self, seed: u64) -> Vec<Fr> {
        match self.workload.as_str() {
            CREDENTIAL => self.credential_public_inputs(seed),
            BATCHED_STATE => self.state_public_inputs(seed),
            PRIVATE_SWAP => self.swap_public_inputs(seed),
            _ => unreachable!("workload checked by build_plan"),
        }
    }

    fn optional_public_inputs(&self) -> Vec<Option<Fr>> {
        match self.seed {
            Some(seed) => self.public_inputs(seed).into_iter().map(Some).collect(),
            None => {
                let count = match self.workload.as_str() {
                    CREDENTIAL => 2,
                    BATCHED_STATE => 3,
                    PRIVATE_SWAP if self.parameters.membership_enabled() => 7,
                    PRIVATE_SWAP => 6,
                    _ => unreachable!("workload checked by build_plan"),
                };
                vec![None; count]
            }
        }
    }

    fn credential_values(seed: u64, index: usize) -> (u64, u64, u64) {
        let age = 18 + (seed.wrapping_add(index as u64 * 17) % 83);
        let subject = seed.wrapping_add(index as u64 * 31).wrapping_add(101);
        let nonce = seed.wrapping_add(index as u64 * 43).wrapping_add(211);
        (age, subject, nonce)
    }

    fn credential_public_inputs(&self, seed: u64) -> Vec<Fr> {
        let min_age = 18_u64;
        let mut aggregate = Fr::from(23_u64);
        for index in 0..self.scale {
            let (age, subject, nonce) = Self::credential_values(seed, index);
            let identity = hash_native(
                Fr::from(subject),
                Fr::from(nonce),
                self.parameters.hash_rounds,
            );
            let commitment = hash_native(
                identity,
                Fr::from(age),
                self.parameters.hash_rounds,
            );
            aggregate = hash_native(
                aggregate,
                commitment,
                self.parameters.hash_rounds,
            );
        }
        vec![Fr::from(min_age), aggregate]
    }

    fn state_delta(seed: u64, index: usize, width: usize) -> u64 {
        let mask = if width >= 63 {
            u64::MAX >> 1
        } else {
            (1_u64 << width) - 1
        };
        2 + seed.wrapping_add(index as u64 * 13) % mask.saturating_sub(2)
    }

    fn state_public_inputs(&self, seed: u64) -> Vec<Fr> {
        let initial = Fr::from(seed.wrapping_add(29));
        let mut state = initial;
        let mut digest = Fr::from(31_u64);
        for index in 0..self.scale {
            let delta = Fr::from(Self::state_delta(
                seed,
                index,
                self.parameters.update_bits,
            ));
            state += delta;
            let update = hash_native(
                delta,
                state,
                self.parameters.hash_rounds,
            );
            digest = hash_native(digest, update, self.parameters.hash_rounds);
        }
        vec![initial, state, digest]
    }

    fn swap_values(seed: u64, index: usize) -> (u64, u64, u64) {
        let amount_a = 100 + 2 * (seed.wrapping_add(index as u64 * 5) % 1_000);
        let amount_b = amount_a * 3 / 2;
        let secret = seed.wrapping_add(index as u64 * 47).wrapping_add(401);
        (amount_a, amount_b, secret)
    }

    fn swap_public_inputs(&self, seed: u64) -> Vec<Fr> {
        let price_num = Fr::from(3_u64);
        let price_den = Fr::from(2_u64);
        let current_time = Fr::from(10_000_u64);
        let expiry = Fr::from(10_600_u64);
        let domain = Fr::from(seed.wrapping_add(503));
        let mut hashlock_aggregate = Fr::from(37_u64);
        let mut root_aggregate = Fr::from(41_u64);
        for index in 0..self.scale {
            let (amount_a, amount_b, secret) = Self::swap_values(seed, index);
            let hashlock = hash_native(
                Fr::from(secret),
                domain,
                self.parameters.hash_rounds,
            );
            hashlock_aggregate = hash_native(
                hashlock_aggregate,
                hashlock,
                self.parameters.hash_rounds,
            );
            if self.parameters.membership_enabled() {
                let leaf = hash_native(
                    Fr::from(amount_a),
                    Fr::from(amount_b),
                    self.parameters.hash_rounds,
                );
                for path in 0..self.parameters.membership_paths {
                    let mut node = hash_native(
                        leaf,
                        Fr::from((path as u64) + 2),
                        self.parameters.hash_rounds,
                    );
                    for level in 0..self.parameters.merkle_depth {
                        let sibling = Fr::from(
                            seed.wrapping_add((index * 101 + path * 17 + level) as u64)
                                .wrapping_add(607),
                        );
                        let direction =
                            ((seed + index as u64 + path as u64 + level as u64) & 1) == 1;
                        node = if direction {
                            hash_native(sibling, node, self.parameters.hash_rounds)
                        } else {
                            hash_native(node, sibling, self.parameters.hash_rounds)
                        };
                    }
                    root_aggregate = hash_native(
                        root_aggregate,
                        node,
                        self.parameters.hash_rounds,
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
        if self.parameters.membership_enabled() {
            inputs.push(root_aggregate);
        }
        inputs
    }

    fn synthesize_credential(
        &self,
        cs: ConstraintSystemRef<Fr>,
    ) -> Result<(), SynthesisError> {
        let public = self.optional_public_inputs();
        let min_age = input_field(cs.clone(), public[0])?;
        let expected_aggregate = input_field(cs.clone(), public[1])?;
        let mut aggregate = FpVar::constant(Fr::from(23_u64));
        for index in 0..self.scale {
            let values = self.seed.map(|seed| Self::credential_values(seed, index));
            let age = bounded_witness(
                cs.clone(),
                values.map(|item| item.0),
                self.parameters.age_bits,
            )?;
            let age_delta = bounded_witness(
                cs.clone(),
                values.map(|item| item.0 - 18),
                self.parameters.age_bits,
            )?;
            age.enforce_equal(&(&min_age + &age_delta))?;
            let subject = witness_field(cs.clone(), values.map(|item| item.1))?;
            let nonce = witness_field(cs.clone(), values.map(|item| item.2))?;
            let authorized = Boolean::new_witness(cs.clone(), || {
                self.seed
                    .map(|_| true)
                    .ok_or(SynthesisError::AssignmentMissing)
            })?;
            authorized.enforce_equal(&Boolean::TRUE)?;
            let identity = hash_gadget(
                &subject,
                &nonce,
                self.parameters.hash_rounds,
            )?;
            let commitment = hash_gadget(
                &identity,
                &age,
                self.parameters.hash_rounds,
            )?;
            aggregate = hash_gadget(
                &aggregate,
                &commitment,
                self.parameters.hash_rounds,
            )?;
        }
        aggregate.enforce_equal(&expected_aggregate)
    }

    fn synthesize_state(
        &self,
        cs: ConstraintSystemRef<Fr>,
    ) -> Result<(), SynthesisError> {
        let public = self.optional_public_inputs();
        let mut state = input_field(cs.clone(), public[0])?;
        let expected_state = input_field(cs.clone(), public[1])?;
        let expected_digest = input_field(cs.clone(), public[2])?;
        let mut digest = FpVar::constant(Fr::from(31_u64));
        for index in 0..self.scale {
            let delta_value = self.seed.map(|seed| {
                Self::state_delta(seed, index, self.parameters.update_bits)
            });
            let delta = bounded_witness(
                cs.clone(),
                delta_value,
                self.parameters.update_bits,
            )?;
            state = &state + &delta;
            let update = hash_gadget(
                &delta,
                &state,
                self.parameters.hash_rounds,
            )?;
            digest = hash_gadget(
                &digest,
                &update,
                self.parameters.hash_rounds,
            )?;
        }
        state.enforce_equal(&expected_state)?;
        digest.enforce_equal(&expected_digest)
    }

    fn synthesize_swap(
        &self,
        cs: ConstraintSystemRef<Fr>,
    ) -> Result<(), SynthesisError> {
        let public = self.optional_public_inputs();
        let price_num = input_field(cs.clone(), public[0])?;
        let price_den = input_field(cs.clone(), public[1])?;
        let current_time = input_field(cs.clone(), public[2])?;
        let expiry = input_field(cs.clone(), public[3])?;
        let domain = input_field(cs.clone(), public[4])?;
        let expected_hashlocks = input_field(cs.clone(), public[5])?;
        let expected_roots = if self.parameters.membership_enabled() {
            Some(input_field(cs.clone(), public[6])?)
        } else {
            None
        };
        let mut hashlock_aggregate = FpVar::constant(Fr::from(37_u64));
        let mut root_aggregate = FpVar::constant(Fr::from(41_u64));
        for index in 0..self.scale {
            let values = self.seed.map(|seed| Self::swap_values(seed, index));
            let amount_a = if self.parameters.range_enabled() {
                bounded_witness(
                    cs.clone(),
                    values.map(|item| item.0),
                    self.parameters.range_bits,
                )?
            } else {
                witness_field(cs.clone(), values.map(|item| item.0))?
            };
            let amount_b = if self.parameters.range_enabled() {
                bounded_witness(
                    cs.clone(),
                    values.map(|item| item.1),
                    self.parameters.range_bits,
                )?
            } else {
                witness_field(cs.clone(), values.map(|item| item.1))?
            };
            if self.parameters.price_enabled() {
                (&amount_a * &price_num)
                    .enforce_equal(&(&amount_b * &price_den))?;
            }
            let secret = witness_field(cs.clone(), values.map(|item| item.2))?;
            let hashlock = hash_gadget(
                &secret,
                &domain,
                self.parameters.hash_rounds,
            )?;
            hashlock_aggregate = hash_gadget(
                &hashlock_aggregate,
                &hashlock,
                self.parameters.hash_rounds,
            )?;
            let remaining = bounded_witness(
                cs.clone(),
                self.seed.map(|_| 600_u64),
                self.parameters.time_bits,
            )?;
            (&current_time + &remaining).enforce_equal(&expiry)?;
            if self.parameters.authorization_enabled() {
                let authorized = Boolean::new_witness(cs.clone(), || {
                    self.seed
                        .map(|_| true)
                        .ok_or(SynthesisError::AssignmentMissing)
                })?;
                authorized.enforce_equal(&Boolean::TRUE)?;
            }
            if self.parameters.membership_enabled() {
                let leaf = hash_gadget(
                    &amount_a,
                    &amount_b,
                    self.parameters.hash_rounds,
                )?;
                for path in 0..self.parameters.membership_paths {
                    let path_tag = FpVar::constant(Fr::from((path as u64) + 2));
                    let mut node = hash_gadget(
                        &leaf,
                        &path_tag,
                        self.parameters.hash_rounds,
                    )?;
                    for level in 0..self.parameters.merkle_depth {
                        let sibling_value = self.seed.map(|seed| {
                            seed.wrapping_add(
                                (index * 101 + path * 17 + level) as u64,
                            )
                            .wrapping_add(607)
                        });
                        let sibling = witness_field(cs.clone(), sibling_value)?;
                        let direction_value = self.seed.map(|seed| {
                            ((seed
                                + index as u64
                                + path as u64
                                + level as u64)
                                & 1)
                                == 1
                        });
                        let direction = Boolean::new_witness(cs.clone(), || {
                            direction_value
                                .ok_or(SynthesisError::AssignmentMissing)
                        })?;
                        let left = direction.select(&sibling, &node)?;
                        let right = direction.select(&node, &sibling)?;
                        node = hash_gadget(
                            &left,
                            &right,
                            self.parameters.hash_rounds,
                        )?;
                    }
                    root_aggregate = hash_gadget(
                        &root_aggregate,
                        &node,
                        self.parameters.hash_rounds,
                    )?;
                }
            }
        }
        hashlock_aggregate.enforce_equal(&expected_hashlocks)?;
        if let Some(expected_roots) = expected_roots {
            root_aggregate.enforce_equal(&expected_roots)?;
        }
        Ok(())
    }
}

impl ConstraintSynthesizer<Fr> for ApplicationCircuit {
    fn generate_constraints(
        self,
        cs: ConstraintSystemRef<Fr>,
    ) -> Result<(), SynthesisError> {
        let target_native_size = self.parameters.target_native_size;
        match self.workload.as_str() {
            CREDENTIAL => self.synthesize_credential(cs.clone())?,
            BATCHED_STATE => self.synthesize_state(cs.clone())?,
            PRIVATE_SWAP => self.synthesize_swap(cs.clone())?,
            _ => return Err(SynthesisError::Unsatisfiable),
        };
        if let Some(target) = target_native_size {
            if cs.num_constraints() > target {
                return Err(SynthesisError::Unsatisfiable);
            }
            while cs.num_constraints() < target {
                cs.enforce_r1cs_constraint(
                    || lc!() + Variable::One,
                    || lc!() + Variable::One,
                    || lc!() + Variable::One,
                )?;
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use ark_relations::gr1cs::ConstraintSystem;

    fn request(workload: &str) -> AdapterRequest {
        AdapterRequest {
            run_id: format!("test-{workload}"),
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
            let plan = build_plan(&request(workload)).unwrap();
            let cs = ConstraintSystem::<Fr>::new_ref();
            plan.circuit.generate_constraints(cs.clone()).unwrap();
            assert!(cs.is_satisfied().unwrap(), "{workload}");
            assert!(cs.num_constraints() > 2, "{workload}");
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
    fn swap_ablation_changes_membership_shape_without_zero_placeholders() {
        let mut value = request(PRIVATE_SWAP);
        value.parameters.insert(
            "ablation".to_owned(),
            "no_membership".into(),
        );
        let plan = build_plan(&value).unwrap();
        let cs = ConstraintSystem::<Fr>::new_ref();
        plan.circuit.generate_constraints(cs.clone()).unwrap();
        assert!(cs.is_satisfied().unwrap());
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
        assert!(build_plan(&value).is_err());
    }

    #[test]
    fn target_native_size_pads_application_exactly() {
        let mut value = request(CREDENTIAL);
        value
            .parameters
            .insert("target_native_size".to_owned(), 4096_u64.into());
        let plan = build_plan(&value).unwrap();
        let cs = ConstraintSystem::<Fr>::new_ref();
        plan.circuit.generate_constraints(cs.clone()).unwrap();
        assert!(cs.is_satisfied().unwrap());
        assert_eq!(cs.num_constraints(), 4096);
    }
}
