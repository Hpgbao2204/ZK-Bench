"""Toolchain-neutral JSON event protocol for proof-system adapters."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Iterable


SCHEMA_VERSION = "1.0"
COARSE_PHASES = {
    "native_execution",
    "setup_or_preprocess",
    "key_load",
    "witness",
    "prove_total",
    "serialize",
    "verify_total",
}
FINE_PHASES = {
    "fft_ntt",
    "msm",
    "commitment",
    "transcript",
    "fri",
    "merkle",
    "deserialize",
    "verify_core",
    "invalid_reject",
}
PHASES = COARSE_PHASES | FINE_PHASES


def is_numeric_boundary(value: float) -> bool:
    return value == 0.0 or value == 1.0


@dataclass(frozen=True)
class AdapterRequest:
    run_id: str
    workload: str
    scale: int
    threads: int
    seed: int
    mode: str = "warm"
    invalid_case: str | None = None
    parameters: dict[str, int | str | bool] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.run_id.strip():
            raise ValueError("run_id must not be empty")
        if not self.workload.strip():
            raise ValueError("workload must not be empty")
        if isinstance(self.scale, bool) or not isinstance(self.scale, int) or self.scale <= 1:
            raise ValueError("scale must exceed excluded boundary values")
        if (
            isinstance(self.threads, bool)
            or not isinstance(self.threads, int)
            or self.threads < 1
        ):
            raise ValueError("threads must be a positive integer")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            raise ValueError("seed must be a nonnegative integer")
        if self.mode not in {"cold", "warm"}:
            raise ValueError("mode must be cold or warm")
        for name, value in self.parameters.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("parameter names must be nonempty strings")
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                if value <= 1:
                    raise ValueError(
                        f"numeric parameter {name} must exceed excluded boundary values"
                    )
                continue
            if isinstance(value, str) and value.strip():
                continue
            raise ValueError(
                f"parameter {name} must be a nonboundary integer, boolean, "
                "or nonempty categorical string"
            )

    def to_json(self) -> str:
        self.validate()
        return json.dumps(self.__dict__, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class PhaseEvent:
    run_id: str
    adapter: str
    phase: str
    supported: bool
    status: str
    thread_count: int
    elapsed_ns: int | None = None
    metrics: dict[str, float | int] = field(default_factory=dict)
    unavailable_reason: str | None = None
    boundary_reason: str | None = None
    schema_version: str = SCHEMA_VERSION
    event_type: str = "phase"

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "PhaseEvent":
        event = cls(**value)
        event.validate()
        return event

    def validate(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema version: {self.schema_version}")
        if self.event_type != "phase":
            raise ValueError(f"unexpected event type: {self.event_type}")
        if self.phase not in PHASES:
            raise ValueError(f"unknown phase: {self.phase}")
        if self.status not in {"ok", "error", "unsupported"}:
            raise ValueError(f"unknown status: {self.status}")
        if self.thread_count < 1:
            raise ValueError("thread_count must be positive")
        if not self.supported:
            if not self.unavailable_reason:
                raise ValueError("unsupported phase requires unavailable_reason")
            if self.elapsed_ns is not None or self.metrics:
                raise ValueError("unsupported phase cannot contain numeric measurements")
            return
        if self.status == "unsupported":
            raise ValueError("supported phase cannot have unsupported status")
        if self.elapsed_ns is None or self.elapsed_ns <= 0:
            raise ValueError("supported phase requires positive elapsed_ns")
        for name, raw in self.metrics.items():
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise ValueError(f"metric {name} must be quantitative, not boolean/text")
            number = float(raw)
            if not math.isfinite(number):
                raise ValueError(f"metric {name} must be finite")
            if is_numeric_boundary(number) and not self.boundary_reason:
                raise ValueError(
                    f"metric {name} equals excluded boundary {number}; "
                    "supply boundary_reason to retain raw evidence"
                )

    def to_json(self) -> str:
        self.validate()
        return json.dumps(self.__dict__, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class AdapterResult:
    run_id: str
    adapter: str
    verify_ok: bool
    proof_bytes: int
    native_work_units: int
    public_inputs: int
    constraints: int
    invalid_case: str | None = None
    error_type: str | None = None
    schema_version: str = SCHEMA_VERSION
    event_type: str = "result"

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "AdapterResult":
        result = cls(**value)
        result.validate()
        return result

    def validate(self) -> None:
        if self.schema_version != SCHEMA_VERSION or self.event_type != "result":
            raise ValueError("invalid adapter result schema")
        if not self.run_id.strip() or not self.adapter.strip():
            raise ValueError("adapter result identifiers must not be empty")
        if not isinstance(self.verify_ok, bool):
            raise ValueError("verify_ok must be boolean")
        for name in ("proof_bytes", "native_work_units", "public_inputs", "constraints"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 1:
                raise ValueError(f"{name} must exceed excluded boundary values")
        if self.verify_ok and self.error_type is not None:
            raise ValueError("successful verification cannot have error_type")
        if not self.verify_ok and not self.error_type:
            raise ValueError("failed verification requires error_type")


AdapterEvent = PhaseEvent | AdapterResult


def parse_json_lines(lines: Iterable[str]) -> list[AdapterEvent]:
    events: list[AdapterEvent] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("adapter event must be a JSON object")
            if value.get("event_type") == "phase":
                events.append(PhaseEvent.from_mapping(value))
            elif value.get("event_type") == "result":
                events.append(AdapterResult.from_mapping(value))
            else:
                raise ValueError(f"unknown event_type: {value.get('event_type')}")
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid adapter event on line {line_number}: {exc}") from exc
    return events
