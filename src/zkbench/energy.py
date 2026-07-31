"""Energy measurement provider interface.

No provider may estimate energy from TDP. Unsupported hosts return an explicit
reason and no numeric value.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class EnergyReading:
    supported: bool
    joules: float | None
    provider: str
    scope: str
    reason: str | None = None


class EnergyProvider(Protocol):
    def start(self) -> object: ...

    def stop(self, token: object) -> EnergyReading: ...


class UnavailableEnergyProvider:
    def __init__(self, reason: str) -> None:
        self.reason = reason

    def start(self) -> object:
        return object()

    def stop(self, token: object) -> EnergyReading:
        del token
        return EnergyReading(
            supported=False,
            joules=None,
            provider="unavailable",
            scope="none",
            reason=self.reason,
        )
