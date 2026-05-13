"""Domain models for the trino_stress skill."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class QueryResult:
    name: str
    elapsed: float | None
    stats: dict
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.elapsed is not None


@dataclass
class RunResult:
    """Timing data for a single concurrent burst."""

    workers: int
    completed: int
    errors: int
    error_msgs: list[str]
    wall: float
    qps: float
    times: list[float]
    p50: float | None
    p95: float | None
    p99: float | None
    max: float | None
    peak_mem_mb: float
    rows: int
    docker_mid: dict = field(default_factory=dict)
    docker_end: dict = field(default_factory=dict)
    cluster_mid: dict = field(default_factory=dict)


@dataclass
class BaselineResult:
    name: str
    times: list[float]
    errors: list[str]
    peak_mem_mb: float


@dataclass
class StressReport:
    """Full output of a trino_stress run."""

    target: str
    scenario: str
    query_source: str = "explicit"      # "explicit" | "discovered" | "builtin"
    discovered_tables: list[str] = field(default_factory=list)   # full_name list
    generated_queries: dict[str, str] = field(default_factory=dict)  # name → SQL
    baseline: dict[str, BaselineResult] = field(default_factory=dict)
    concurrency: dict[int, RunResult] = field(default_factory=dict)
    mixed: dict = field(default_factory=dict)
    memory: dict[str, RunResult] = field(default_factory=dict)
    breaking: dict[int, RunResult] = field(default_factory=dict)
