"""Execute one adapter process and validate its toolchain-neutral transcript."""

from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Sequence

from .adapter_protocol import (
    COARSE_PHASES,
    AdapterEvent,
    AdapterRequest,
    AdapterResult,
    PhaseEvent,
    parse_json_lines,
)
from .system_metrics import ProcessCounters, default_process_counter_provider


@dataclass(frozen=True)
class ProcessMeasurements:
    peak_rss_bytes: int | None
    peak_private_bytes: int | None
    page_faults: int | None
    process_read_bytes: int | None
    process_write_bytes: int | None
    peak_swap_bytes: int | None
    cpu_time_ns: int | None
    provider: str
    unavailable_reason: str | None
    sampling_interval_ms: float
    samples: int


@dataclass(frozen=True)
class AdapterExecution:
    command: tuple[str, ...]
    request: AdapterRequest
    events: tuple[AdapterEvent, ...]
    stdout: str
    stderr: str
    exit_code: int
    wall_time_ns: int
    timed_out: bool
    protocol_error: str | None
    process: ProcessMeasurements

    @property
    def result(self) -> AdapterResult | None:
        results = [event for event in self.events if isinstance(event, AdapterResult)]
        return results[0] if len(results) == 1 else None

    @property
    def phases(self) -> tuple[PhaseEvent, ...]:
        return tuple(event for event in self.events if isinstance(event, PhaseEvent))

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and self.protocol_error is None


class _CounterAccumulator:
    _COUNTERS = (
        "peak_rss_bytes",
        "private_bytes",
        "page_faults",
        "read_bytes",
        "write_bytes",
        "swap_bytes",
        "cpu_time_ns",
    )

    def __init__(self, sampling_interval_ms: float) -> None:
        self.sampling_interval_ms = sampling_interval_ms
        self.maxima: dict[str, int | None] = {name: None for name in self._COUNTERS}
        self.providers: set[str] = set()
        self.reasons: set[str] = set()
        self.samples = 0

    def add(self, counters: ProcessCounters) -> None:
        if counters.supported:
            self.samples += 1
            self.providers.add(counters.provider)
            for name in self._COUNTERS:
                value = getattr(counters, name)
                if value is not None:
                    current = self.maxima[name]
                    self.maxima[name] = value if current is None else max(current, value)
        if counters.unavailable_reason:
            self.reasons.add(counters.unavailable_reason)

    def finish(self) -> ProcessMeasurements:
        provider = ",".join(sorted(self.providers)) if self.providers else "unavailable"
        reason = "; ".join(sorted(self.reasons)) or None
        return ProcessMeasurements(
            peak_rss_bytes=self.maxima["peak_rss_bytes"],
            peak_private_bytes=self.maxima["private_bytes"],
            page_faults=self.maxima["page_faults"],
            process_read_bytes=self.maxima["read_bytes"],
            process_write_bytes=self.maxima["write_bytes"],
            peak_swap_bytes=self.maxima["swap_bytes"],
            cpu_time_ns=self.maxima["cpu_time_ns"],
            provider=provider,
            unavailable_reason=reason,
            sampling_interval_ms=self.sampling_interval_ms,
            samples=self.samples,
        )


def validate_transcript(
    request: AdapterRequest, events: Sequence[AdapterEvent]
) -> None:
    phases = [event for event in events if isinstance(event, PhaseEvent)]
    results = [event for event in events if isinstance(event, AdapterResult)]
    if len(results) != 1:
        raise ValueError(f"adapter transcript requires one result; received {len(results)}")
    result = results[0]
    if result.run_id != request.run_id:
        raise ValueError("adapter result run_id does not match request")
    if result.native_work_units != request.scale:
        raise ValueError("adapter result native_work_units does not match request scale")
    if request.invalid_case is None and not result.verify_ok:
        raise ValueError("valid request did not verify")
    if request.invalid_case is not None and result.verify_ok:
        raise ValueError("invalid request unexpectedly verified")

    phase_counts: dict[str, int] = {}
    adapters = {result.adapter}
    for event in phases:
        if event.run_id != request.run_id:
            raise ValueError("phase run_id does not match request")
        if event.thread_count != request.threads:
            raise ValueError("phase thread_count does not match request")
        adapters.add(event.adapter)
        phase_counts[event.phase] = phase_counts.get(event.phase, 0) + 1
    duplicates = sorted(phase for phase, count in phase_counts.items() if count > 1)
    if duplicates:
        raise ValueError(f"duplicate adapter phases: {', '.join(duplicates)}")
    missing = sorted(COARSE_PHASES - phase_counts.keys())
    if missing:
        raise ValueError(f"missing coarse adapter phases: {', '.join(missing)}")
    if len(adapters) != 1:
        raise ValueError("adapter identifier changed within transcript")


def execute_adapter(
    command: Sequence[str],
    request: AdapterRequest,
    *,
    timeout_seconds: float = 300.0,
    sampling_interval_ms: float = 10.0,
) -> AdapterExecution:
    request.validate()
    if not command:
        raise ValueError("adapter command must not be empty")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if sampling_interval_ms <= 0:
        raise ValueError("sampling_interval_ms must be positive")

    wall_start = time.perf_counter_ns()
    process = subprocess.Popen(
        list(command),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    provider = default_process_counter_provider()
    accumulator = _CounterAccumulator(sampling_interval_ms)
    stop = threading.Event()

    def sample() -> None:
        while True:
            accumulator.add(provider.capture(process.pid))
            if stop.wait(sampling_interval_ms / 1000):
                return

    sampler = threading.Thread(target=sample, name="zkbench-process-sampler", daemon=True)
    sampler.start()
    timed_out = False
    try:
        stdout, stderr = process.communicate(
            input=request.to_json(), timeout=timeout_seconds
        )
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        stdout, stderr = process.communicate()
    finally:
        stop.set()
        sampler.join()
    wall_time_ns = time.perf_counter_ns() - wall_start

    events: tuple[AdapterEvent, ...] = ()
    protocol_error: str | None = None
    try:
        events = tuple(parse_json_lines(stdout.splitlines()))
        if process.returncode == 0 and not timed_out:
            validate_transcript(request, events)
    except ValueError as error:
        protocol_error = str(error)
    if timed_out:
        protocol_error = f"adapter timed out after {timeout_seconds:g} seconds"
    elif process.returncode != 0 and protocol_error is None:
        protocol_error = f"adapter exited with code {process.returncode}"

    return AdapterExecution(
        command=tuple(command),
        request=request,
        events=events,
        stdout=stdout,
        stderr=stderr,
        exit_code=int(process.returncode),
        wall_time_ns=wall_time_ns,
        timed_out=timed_out,
        protocol_error=protocol_error,
        process=accumulator.finish(),
    )
