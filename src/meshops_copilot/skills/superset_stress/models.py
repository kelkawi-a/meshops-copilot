"""Domain models for the superset_stress skill."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ChartResult:
    """Result of a single POST /api/v1/chart/data call."""

    chart_id: int
    name: str
    elapsed: float | None
    stats: dict
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.elapsed is not None


@dataclass
class DashboardRunResult:
    """Timing data for a single concurrent burst of chart requests."""

    workers: int
    completed: int
    errors: int
    error_msgs: list[str]
    wall: float
    rps: float          # successful requests per second
    times: list[float]
    p50: float | None
    p95: float | None
    p99: float | None
    max: float | None
    docker_mid: dict = field(default_factory=dict)
    docker_end: dict = field(default_factory=dict)


@dataclass
class BaselineChartResult:
    """Serial timing data for a single chart across multiple runs."""

    chart_id: int
    name: str
    times: list[float]
    errors: list[str]

    @property
    def median(self) -> float | None:
        if not self.times:
            return None
        s = sorted(self.times)
        mid = len(s) // 2
        return (s[mid - 1] + s[mid]) / 2 if len(s) % 2 == 0 else s[mid]


@dataclass
class SupersetStressReport:
    """Full output of a superset_stress run."""

    target: str
    scenario: str
    chart_source: str = "builtin"       # "builtin" | "scenario" | "file"
    baseline: dict[str, BaselineChartResult] = field(default_factory=dict)
    concurrency: dict[int, DashboardRunResult] = field(default_factory=dict)
    breaking: dict[int, DashboardRunResult] = field(default_factory=dict)
