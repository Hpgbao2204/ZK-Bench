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


def parse_json_lines(lines: Iterable[str]) -> list[PhaseEvent]:
    events: list[PhaseEvent] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
            events.append(PhaseEvent.from_mapping(value))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid adapter event on line {line_number}: {exc}") from exc
    return events
