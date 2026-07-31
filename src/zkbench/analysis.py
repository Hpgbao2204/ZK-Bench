"""Scientific derived metrics for scaling and parallelism experiments."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from statistics import median
from typing import Callable, Iterable, Mapping, Sequence

from .adapter_protocol import is_numeric_boundary


@dataclass(frozen=True)
class PowerLawFit:
    coefficient_a: float
    exponent_b: float
    r_squared: float | None
    sample_count: int
    min_scale: float
    max_scale: float


@dataclass(frozen=True)
class ParallelPoint:
    threads: int
    median_latency_ms: float
    speedup: float | None
    efficiency: float | None
    marginal_throughput_gain: float


@dataclass(frozen=True)
class ParallelProfile:
    points: tuple[ParallelPoint, ...]
    saturation_threads: int | None
    saturation_threshold: float


def _positive_nonboundary(value: float, name: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be finite and positive")
    if is_numeric_boundary(number):
        raise ValueError(f"{name} cannot be an exact 0.0/1.0 boundary value")
    return number


def _derived_or_none(value: float) -> float | None:
    if not math.isfinite(value) or is_numeric_boundary(value):
        return None
    return value


def fit_power_law(samples: Iterable[tuple[float, float]]) -> PowerLawFit:
    points = [
        (_positive_nonboundary(scale, "scale"), _positive_nonboundary(runtime, "runtime"))
        for scale, runtime in samples
    ]
    if len(points) < 3:
        raise ValueError("power-law fit requires at least three scale points")
    xs = [math.log(scale) for scale, _ in points]
    ys = [math.log(runtime) for _, runtime in points]
    x_mean = sum(xs) / len(xs)
    y_mean = sum(ys) / len(ys)
    denominator = sum((x - x_mean) ** 2 for x in xs)
    if denominator == 0:
        raise ValueError("scale points must not all be equal")
    exponent = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / denominator
    intercept = y_mean - exponent * x_mean
    predicted = [intercept + exponent * x for x in xs]
    total = sum((y - y_mean) ** 2 for y in ys)
    residual = sum((y - estimate) ** 2 for y, estimate in zip(ys, predicted))
    r_squared = 1.0 - residual / total if total > 0 else float("nan")
    return PowerLawFit(
        coefficient_a=math.exp(intercept),
        exponent_b=exponent,
        r_squared=_derived_or_none(r_squared),
        sample_count=len(points),
        min_scale=min(scale for scale, _ in points),
        max_scale=max(scale for scale, _ in points),
    )


def parallel_profile(
    latency_samples_ms: Mapping[int, Iterable[float]],
    saturation_threshold: float = 0.10,
) -> ParallelProfile:
    if 1 not in latency_samples_ms:
        raise ValueError("one-thread baseline is required")
    if not 0 < saturation_threshold < 1:
        raise ValueError("saturation threshold must be between zero and one")
    medians = {
        int(threads): median(
            _positive_nonboundary(value, "latency_ms") for value in values
        )
        for threads, values in latency_samples_ms.items()
    }
    if any(threads < 1 for threads in medians):
        raise ValueError("thread counts must be positive")
    baseline = medians[1]
    points: list[ParallelPoint] = []
    previous_throughput = 1000.0 / baseline
    low_gain_candidate: int | None = None
    saturation_threads: int | None = None
    for threads in sorted(medians):
        if threads == 1:
            continue
        runtime = medians[threads]
        speedup = _derived_or_none(baseline / runtime)
        efficiency = _derived_or_none((baseline / runtime) / threads)
        throughput = 1000.0 / runtime
        marginal_gain = (throughput - previous_throughput) / previous_throughput
        points.append(
            ParallelPoint(
                threads=threads,
                median_latency_ms=runtime,
                speedup=speedup,
                efficiency=efficiency,
                marginal_throughput_gain=marginal_gain,
            )
        )
        if marginal_gain < saturation_threshold:
            if low_gain_candidate is None:
                low_gain_candidate = threads
            elif saturation_threads is None:
                saturation_threads = low_gain_candidate
        else:
            low_gain_candidate = None
        previous_throughput = throughput
    return ParallelProfile(tuple(points), saturation_threads, saturation_threshold)


def native_overhead(prover_ms: float, native_ms: float) -> float | None:
    prover = _positive_nonboundary(prover_ms, "prover_ms")
    native = _positive_nonboundary(native_ms, "native_ms")
    return _derived_or_none(prover / native)


def application_throughput(units: int, runtime_ms: float) -> float | None:
    if units <= 0:
        raise ValueError("application units must be positive")
    runtime = _positive_nonboundary(runtime_ms, "runtime_ms")
    return _derived_or_none(units * 1000.0 / runtime)


def bootstrap_interval(
    values: Sequence[float],
    statistic: Callable[[Sequence[float]], float],
    *,
    seed: int,
    resamples: int = 2_000,
    confidence: float = 0.95,
) -> tuple[float, float]:
    clean = [_positive_nonboundary(value, "bootstrap value") for value in values]
    if len(clean) < 3:
        raise ValueError("bootstrap interval requires at least three observations")
    if resamples < 100:
        raise ValueError("use at least 100 bootstrap resamples")
    if not 0 < confidence < 1:
        raise ValueError("confidence must be between zero and one")
    rng = random.Random(seed)
    estimates = sorted(
        statistic([rng.choice(clean) for _ in clean]) for _ in range(resamples)
    )
    tail = (1.0 - confidence) / 2.0
    lower_index = max(0, int(tail * resamples))
    upper_index = min(resamples - 1, int((1.0 - tail) * resamples) - 1)
    return estimates[lower_index], estimates[upper_index]


def bootstrap_speedup_interval(
    baseline_ms: Sequence[float],
    comparison_ms: Sequence[float],
    *,
    seed: int,
    resamples: int = 2_000,
    confidence: float = 0.95,
) -> tuple[float, float]:
    baseline = [_positive_nonboundary(value, "baseline_ms") for value in baseline_ms]
    comparison = [_positive_nonboundary(value, "comparison_ms") for value in comparison_ms]
    if len(baseline) < 3 or len(comparison) < 3:
        raise ValueError("speedup interval requires at least three observations per group")
    rng = random.Random(seed)
    estimates = []
    for _ in range(resamples):
        sampled_baseline = [rng.choice(baseline) for _ in baseline]
        sampled_comparison = [rng.choice(comparison) for _ in comparison]
        estimate = median(sampled_baseline) / median(sampled_comparison)
        if not is_numeric_boundary(estimate):
            estimates.append(estimate)
    if len(estimates) < 100:
        raise ValueError("too few non-boundary bootstrap estimates")
    estimates.sort()
    tail = (1.0 - confidence) / 2.0
    lower_index = max(0, int(tail * len(estimates)))
    upper_index = min(len(estimates) - 1, int((1.0 - tail) * len(estimates)) - 1)
    return estimates[lower_index], estimates[upper_index]
