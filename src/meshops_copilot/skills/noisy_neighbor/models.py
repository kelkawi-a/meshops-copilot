"""Domain models for the noisy_neighbor skill."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Severity(str, Enum):
    """Noise severity classification."""

    CRITICAL = "critical"   # noise_ratio >= 3.0
    MODERATE = "moderate"   # noise_ratio >= 1.5
    NORMAL = "normal"       # noise_ratio < 1.5

    @classmethod
    def from_ratio(cls, ratio: float) -> "Severity":
        if ratio >= 3.0:
            return cls.CRITICAL
        if ratio >= 1.5:
            return cls.MODERATE
        return cls.NORMAL


@dataclass
class EntityScore:
    """Score for a single entity within a dimension (e.g. one dashboard)."""

    name: str
    activity_count: int         # number of views / queries / log events
    activity_share: float       # fraction of total activity (0.0–1.0)
    cost_ms: float              # total cost in milliseconds (query duration)
    cost_share: float           # fraction of total cost (0.0–1.0)
    noise_ratio: float          # cost_share / activity_share
    severity: Severity
    detail: str = ""            # human-readable sentence

    @classmethod
    def compute(
        cls,
        name: str,
        activity_count: int,
        total_activity: int,
        cost_ms: float,
        total_cost_ms: float,
    ) -> "EntityScore":
        activity_share = activity_count / total_activity if total_activity else 0.0
        cost_share = cost_ms / total_cost_ms if total_cost_ms else 0.0
        noise_ratio = cost_share / activity_share if activity_share > 0 else 0.0
        severity = Severity.from_ratio(noise_ratio)
        detail = (
            f"{name} accounts for {activity_share * 100:.1f}% of activity "
            f"but {cost_share * 100:.1f}% of query time "
            f"(noise ratio: {noise_ratio:.1f}x)"
        )
        return cls(
            name=name,
            activity_count=activity_count,
            activity_share=activity_share,
            cost_ms=cost_ms,
            cost_share=cost_share,
            noise_ratio=noise_ratio,
            severity=severity,
            detail=detail,
        )


@dataclass
class DimensionResult:
    """Analysis result for one dimension (e.g. 'dashboard', 'user')."""

    dimension: str
    total_activity: int = 0
    total_cost_ms: float = 0.0
    entities: list[EntityScore] = field(default_factory=list)

    @property
    def noisy_count(self) -> int:
        """Number of entities with severity >= MODERATE."""
        return sum(1 for e in self.entities if e.severity != Severity.NORMAL)

    @property
    def top_offenders(self) -> list[EntityScore]:
        """Entities sorted by noise_ratio descending, only those >= MODERATE."""
        return sorted(
            [e for e in self.entities if e.severity != Severity.NORMAL],
            key=lambda e: e.noise_ratio,
            reverse=True,
        )


@dataclass
class TrinoQueryRecord:
    """A single Trino query from system.runtime.queries."""

    query_id: str
    user: str
    source: str | None
    state: str
    duration_ms: float
    queued_time_ms: float
    planning_time_ms: float
    query_prefix: str       # first N chars of the SQL
    created: str


@dataclass
class SupersetQueryRecord:
    """A single query from Superset's /api/v1/query/."""

    id: int
    user: str
    user_id: int
    database: str
    schema: str | None
    tables: list[str]       # fully qualified table references
    duration_ms: float
    status: str
    trino_query_id: str | None   # extracted from tracking_url
    start_time: str


@dataclass
class SupersetLogRecord:
    """A single activity record from Superset's /api/v1/log/."""

    action: str
    dashboard_id: int | None
    slice_id: int | None        # chart ID
    user_id: int | None
    user_name: str | None
    duration_ms: float
    dttm: str


@dataclass
class NoisyNeighborReport:
    """Full output of a noisy_neighbor analysis."""

    target_superset: str
    target_trino: str
    lookback_hours: int
    superset_queries_collected: int = 0
    superset_logs_collected: int = 0
    trino_queries_collected: int = 0
    correlated_queries: int = 0
    dimensions: dict[str, DimensionResult] = field(default_factory=dict)
    findings: list[str] = field(default_factory=list)     # top-line sentences
    errors: list[str] = field(default_factory=list)
