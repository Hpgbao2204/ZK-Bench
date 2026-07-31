use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::io::{self, Read};
use std::time::{Duration, Instant};

pub const SCHEMA_VERSION: &str = "1.0";

#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
pub struct AdapterRequest {
    pub run_id: String,
    pub workload: String,
    pub scale: u64,
    pub threads: usize,
    pub seed: u64,
    #[serde(default = "default_mode")]
    pub mode: String,
    #[serde(default)]
    pub invalid_case: Option<String>,
    #[serde(default)]
    pub parameters: BTreeMap<String, serde_json::Value>,
}

fn default_mode() -> String {
    "warm".to_owned()
}

impl AdapterRequest {
    pub fn validate(&self) -> Result<(), String> {
        if self.run_id.trim().is_empty() {
            return Err("run_id must not be empty".to_owned());
        }
        if self.workload.trim().is_empty() {
            return Err("workload must not be empty".to_owned());
        }
        if self.scale <= 1 {
            return Err("scale must exceed excluded boundary values".to_owned());
        }
        if self.threads == 0 {
            return Err("threads must be positive".to_owned());
        }
        if !matches!(self.mode.as_str(), "cold" | "warm") {
            return Err("mode must be cold or warm".to_owned());
        }
        for (name, value) in &self.parameters {
            if name.trim().is_empty() {
                return Err("parameter names must be nonempty strings".to_owned());
            }
            match value {
                serde_json::Value::Bool(_) => {}
                serde_json::Value::Number(number) => {
                    let value = number.as_u64().ok_or_else(|| {
                        format!("numeric parameter {name} must be a nonnegative integer")
                    })?;
                    if value <= 1 {
                        return Err(format!(
                            "numeric parameter {name} must exceed excluded boundary values"
                        ));
                    }
                }
                serde_json::Value::String(text) if !text.trim().is_empty() => {}
                _ => {
                    return Err(format!(
                        "parameter {name} must be a nonboundary integer, boolean, \
                         or nonempty categorical string"
                    ));
                }
            }
        }
        Ok(())
    }
}

#[derive(Clone, Debug, Serialize, PartialEq)]
pub struct PhaseEvent {
    pub schema_version: &'static str,
    pub event_type: &'static str,
    pub run_id: String,
    pub adapter: String,
    pub phase: String,
    pub supported: bool,
    pub status: String,
    pub thread_count: usize,
    pub elapsed_ns: Option<u64>,
    pub metrics: BTreeMap<String, f64>,
    pub unavailable_reason: Option<String>,
    pub boundary_reason: Option<String>,
}

impl PhaseEvent {
    pub fn measured(
        request: &AdapterRequest,
        adapter: &str,
        phase: &str,
        elapsed: Duration,
        metrics: BTreeMap<String, f64>,
    ) -> Result<Self, String> {
        let elapsed_ns = u64::try_from(elapsed.as_nanos())
            .map_err(|_| "elapsed duration exceeds u64 nanoseconds".to_owned())?;
        if elapsed_ns == 0 {
            return Err("measured phase duration must be positive".to_owned());
        }
        for (name, value) in &metrics {
            if !value.is_finite() {
                return Err(format!("metric {name} must be finite"));
            }
            if *value == 0.0 || *value == 1.0 {
                return Err(format!(
                    "metric {name} equals excluded boundary {value}; \
                     retain it only in raw adapter-specific evidence with a reason"
                ));
            }
        }
        Ok(Self {
            schema_version: SCHEMA_VERSION,
            event_type: "phase",
            run_id: request.run_id.clone(),
            adapter: adapter.to_owned(),
            phase: phase.to_owned(),
            supported: true,
            status: "ok".to_owned(),
            thread_count: request.threads,
            elapsed_ns: Some(elapsed_ns),
            metrics,
            unavailable_reason: None,
            boundary_reason: None,
        })
    }

    pub fn unsupported(request: &AdapterRequest, adapter: &str, phase: &str, reason: &str) -> Self {
        Self {
            schema_version: SCHEMA_VERSION,
            event_type: "phase",
            run_id: request.run_id.clone(),
            adapter: adapter.to_owned(),
            phase: phase.to_owned(),
            supported: false,
            status: "unsupported".to_owned(),
            thread_count: request.threads,
            elapsed_ns: None,
            metrics: BTreeMap::new(),
            unavailable_reason: Some(reason.to_owned()),
            boundary_reason: None,
        }
    }

    pub fn to_json_line(&self) -> Result<String, serde_json::Error> {
        serde_json::to_string(self)
    }
}

pub struct PhaseTimer {
    start: Instant,
}

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub struct AdapterResult {
    pub schema_version: &'static str,
    pub event_type: &'static str,
    pub run_id: String,
    pub adapter: String,
    pub verify_ok: bool,
    pub proof_bytes: u64,
    pub native_work_units: u64,
    pub public_inputs: u64,
    pub constraints: u64,
    pub invalid_case: Option<String>,
    pub error_type: Option<String>,
}

impl AdapterResult {
    pub fn to_json_line(&self) -> Result<String, serde_json::Error> {
        serde_json::to_string(self)
    }
}

impl PhaseTimer {
    pub fn start() -> Self {
        Self {
            start: Instant::now(),
        }
    }

    pub fn elapsed(&self) -> Duration {
        self.start.elapsed()
    }
}

pub fn read_request_from_stdin() -> Result<AdapterRequest, String> {
    let mut input = String::new();
    io::stdin()
        .read_to_string(&mut input)
        .map_err(|error| format!("failed to read adapter request: {error}"))?;
    let request: AdapterRequest = serde_json::from_str(&input)
        .map_err(|error| format!("invalid adapter request JSON: {error}"))?;
    request.validate()?;
    Ok(request)
}

pub fn emit(event: &PhaseEvent) -> Result<(), String> {
    println!(
        "{}",
        event
            .to_json_line()
            .map_err(|error| format!("failed to serialize phase event: {error}"))?
    );
    Ok(())
}

pub fn emit_result(result: &AdapterResult) -> Result<(), String> {
    println!(
        "{}",
        result
            .to_json_line()
            .map_err(|error| format!("failed to serialize adapter result: {error}"))?
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn request() -> AdapterRequest {
        AdapterRequest {
            run_id: "run-7".to_owned(),
            workload: "controlled_kernel".to_owned(),
            scale: 1024,
            threads: 4,
            seed: 7,
            mode: "warm".to_owned(),
            invalid_case: None,
            parameters: BTreeMap::new(),
        }
    }

    #[test]
    fn request_rejects_boundary_scale() {
        let mut value = request();
        value.scale = 1;
        assert!(value.validate().is_err());
    }

    #[test]
    fn request_accepts_sensitivity_parameters_and_rejects_numeric_boundaries() {
        let mut value = request();
        value.parameters.insert(
            "merkle_depth".to_owned(),
            serde_json::Value::from(32_u64),
        );
        value.parameters.insert(
            "ablation".to_owned(),
            serde_json::Value::from("full"),
        );
        value.parameters.insert(
            "membership_enabled".to_owned(),
            serde_json::Value::from(true),
        );
        assert!(value.validate().is_ok());
        value
            .parameters
            .insert("range_bits".to_owned(), serde_json::Value::from(1_u64));
        assert!(value.validate().is_err());
    }

    #[test]
    fn event_uses_common_schema() {
        let event = PhaseEvent::measured(
            &request(),
            "test-adapter",
            "witness",
            Duration::from_nanos(25),
            BTreeMap::new(),
        )
        .unwrap();
        let json = event.to_json_line().unwrap();
        assert!(json.contains("\"schema_version\":\"1.0\""));
        assert!(json.contains("\"thread_count\":4"));
    }

    #[test]
    fn unsupported_phase_has_no_numeric_placeholder() {
        let event = PhaseEvent::unsupported(
            &request(),
            "test-adapter",
            "fft_ntt",
            "phase hook unavailable",
        );
        assert_eq!(event.elapsed_ns, None);
        assert!(event.metrics.is_empty());
    }

    #[test]
    fn result_keeps_verification_as_boolean() {
        let result = AdapterResult {
            schema_version: SCHEMA_VERSION,
            event_type: "result",
            run_id: "run-7".to_owned(),
            adapter: "test-adapter".to_owned(),
            verify_ok: true,
            proof_bytes: 128,
            native_work_units: 1024,
            public_inputs: 2,
            constraints: 1024,
            invalid_case: None,
            error_type: None,
        };
        let json = result.to_json_line().unwrap();
        assert!(json.contains("\"verify_ok\":true"));
        assert!(!json.contains("\"verify_ok\":1"));
    }
}
