"""Execute one adapter process and validate its toolchain-neutral transcript."""

from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Mapping, Sequence

from .adapter_protocol import (
    COARSE_PHASES,
    AdapterEvent,
    AdapterRequest,
    AdapterResult,
    PhaseEvent,
    parse_json_lines,
)
from .system_metrics import (
    ProcessCounters,
    SystemMemoryCounters,
    default_process_counter_provider,
    default_system_memory_counter_provider,
)


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
class SystemMemoryMeasurements:
    mem_total_bytes: int | None
    swap_total_bytes: int | None
    mem_available_start_bytes: int | None
    mem_available_end_bytes: int | None
    mem_available_min_bytes: int | None
    swap_used_start_bytes: int | None
    swap_used_end_bytes: int | None
    swap_used_peak_bytes: int | None
    swap_in_pages_delta: int | None
    swap_out_pages_delta: int | None
    swap_in_bytes_delta: int | None
    swap_out_bytes_delta: int | None
    swap_io_observed: bool | None
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
    system_memory: SystemMemoryMeasurements

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


class _SystemMemoryAccumulator:
    def __init__(self, sampling_interval_ms: float) -> None:
        self.sampling_interval_ms = sampling_interval_ms
        self.first: SystemMemoryCounters | None = None
        self.last: SystemMemoryCounters | None = None
        self.mem_available_min_bytes: int | None = None
        self.swap_used_peak_bytes: int | None = None
        self.providers: set[str] = set()
        self.reasons: set[str] = set()
        self.samples = 0

    def add(self, counters: SystemMemoryCounters) -> None:
        if counters.supported:
            self.samples += 1
            self.providers.add(counters.provider)
            if self.first is None:
                self.first = counters
            self.last = counters
            if counters.mem_available_bytes is not None:
                current = self.mem_available_min_bytes
                self.mem_available_min_bytes = (
                    counters.mem_available_bytes
                    if current is None
                    else min(current, counters.mem_available_bytes)
                )
            if (
                counters.swap_total_bytes is not None
                and counters.swap_free_bytes is not None
            ):
                swap_used = counters.swap_total_bytes - counters.swap_free_bytes
                current = self.swap_used_peak_bytes
                self.swap_used_peak_bytes = (
                    swap_used if current is None else max(current, swap_used)
                )
        if counters.unavailable_reason:
            self.reasons.add(counters.unavailable_reason)

    @staticmethod
    def _delta(start: int | None, end: int | None) -> int | None:
        if start is None or end is None or end < start:
            return None
        return end - start

    @staticmethod
    def _swap_used(counters: SystemMemoryCounters | None) -> int | None:
        if (
            counters is None
            or counters.swap_total_bytes is None
            or counters.swap_free_bytes is None
        ):
            return None
        return counters.swap_total_bytes - counters.swap_free_bytes

    def finish(self) -> SystemMemoryMeasurements:
        first = self.first
        last = self.last
        swap_in_pages = self._delta(
            first.swap_in_pages if first else None,
            last.swap_in_pages if last else None,
        )
        swap_out_pages = self._delta(
            first.swap_out_pages if first else None,
            last.swap_out_pages if last else None,
        )
        page_size = (
            first.page_size_bytes
            if first and first.page_size_bytes is not None
            else (last.page_size_bytes if last else None)
        )
        swap_in_bytes = (
            swap_in_pages * page_size
            if swap_in_pages is not None and page_size is not None
            else None
        )
        swap_out_bytes = (
            swap_out_pages * page_size
            if swap_out_pages is not None and page_size is not None
            else None
        )
        observed = (
            (swap_in_pages > 0 or swap_out_pages > 0)
            if swap_in_pages is not None and swap_out_pages is not None
            else None
        )
        reason = "; ".join(sorted(self.reasons)) or None
        if self.samples < 2:
            missing = "requires at least two system-memory snapshots"
            reason = f"{reason}; {missing}" if reason else missing
        return SystemMemoryMeasurements(
            mem_total_bytes=first.mem_total_bytes if first else None,
            swap_total_bytes=first.swap_total_bytes if first else None,
            mem_available_start_bytes=first.mem_available_bytes if first else None,
            mem_available_end_bytes=last.mem_available_bytes if last else None,
            mem_available_min_bytes=self.mem_available_min_bytes,
            swap_used_start_bytes=self._swap_used(first),
            swap_used_end_bytes=self._swap_used(last),
            swap_used_peak_bytes=self.swap_used_peak_bytes,
            swap_in_pages_delta=swap_in_pages,
            swap_out_pages_delta=swap_out_pages,
            swap_in_bytes_delta=swap_in_bytes,
            swap_out_bytes_delta=swap_out_bytes,
            swap_io_observed=observed,
            provider=",".join(sorted(self.providers)) if self.providers else "unavailable",
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
    scale_mode = request.parameters.get("scale_mode")
    if scale_mode == "target_native_size":
        if result.native_work_units != request.scale:
            raise ValueError("adapter result native_work_units does not match target scale")
    elif scale_mode == "application_units":
        native_events = [
            event
            for event in phases
            if event.phase == "native_execution" and event.supported
        ]
        application_units = (
            native_events[0].metrics.get("application_units")
            if len(native_events) == 1
            else None
        )
        if application_units != request.scale:
            raise ValueError(
                "application-scale request requires native_execution.application_units "
                "to match request scale"
            )
    elif result.native_work_units != request.scale:
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
    system_sampling_interval_ms: float = 250.0,
    environment: Mapping[str, str] | None = None,
) -> AdapterExecution:
    request.validate()
    if not command:
        raise ValueError("adapter command must not be empty")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if sampling_interval_ms <= 0:
        raise ValueError("sampling_interval_ms must be positive")
    if system_sampling_interval_ms <= 0:
        raise ValueError("system_sampling_interval_ms must be positive")

    system_provider = default_system_memory_counter_provider()
    system_accumulator = _SystemMemoryAccumulator(system_sampling_interval_ms)
    system_accumulator.add(system_provider.capture())
    wall_start = time.perf_counter_ns()
    process = subprocess.Popen(
        list(command),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=None if environment is None else dict(environment),
    )
    provider = default_process_counter_provider()
    accumulator = _CounterAccumulator(sampling_interval_ms)
    stop = threading.Event()

    def sample() -> None:
        while True:
            accumulator.add(provider.capture(process.pid))
            if stop.wait(sampling_interval_ms / 1000):
                return

    def sample_system_memory() -> None:
        while True:
            system_accumulator.add(system_provider.capture())
            if stop.wait(system_sampling_interval_ms / 1000):
                return

    sampler = threading.Thread(target=sample, name="zkbench-process-sampler", daemon=True)
    system_sampler = threading.Thread(
        target=sample_system_memory,
        name="zkbench-system-memory-sampler",
        daemon=True,
    )
    sampler.start()
    system_sampler.start()
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
        system_sampler.join()
        system_accumulator.add(system_provider.capture())
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
        system_memory=system_accumulator.finish(),
    )
