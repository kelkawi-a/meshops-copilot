"""grafana_diagnostics.models — data models for diagnostics results."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass
class MetricSample:
    """A single metric observation with labels and value."""

    metric: str
    labels: dict[str, str]
    value: float
    timestamp: float | None = None


@dataclass
class CategoryResult:
    """Analysis result for one diagnostic category (e.g. CPU, memory, latency)."""

    category: str
    top_consumers: list[MetricSample] = field(default_factory=list)
    summary: str = ""
    raw_queries: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class LogEntry:
    """A single log line with metadata."""

    timestamp: str
    line: str
    labels: dict[str, str] = field(default_factory=dict)


@dataclass
class LogResult:
    """Log analysis results for a component."""

    component: str = ""
    total_lines: int = 0
    error_lines: list[LogEntry] = field(default_factory=list)
    warning_lines: list[LogEntry] = field(default_factory=list)
    sample_lines: list[LogEntry] = field(default_factory=list)
    error_rate: float = 0.0  # errors per second
    patterns: list[dict] = field(default_factory=list)  # detected log patterns from Loki
    raw_queries: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    summary: str = ""


@dataclass
class Bottleneck:
    """A detected performance bottleneck."""

    rank: int
    component: str
    severity: Severity
    root_cause: str
    source: str = ""  # "metrics" or "logs"
    metric: str = ""
    value: float = 0.0


@dataclass
class RecommendedAction:
    """A specific tuning recommendation."""

    action: str
    reason: str
    effort: str  # S, M, L


@dataclass
class DiagnosticsReport:
    """The full diagnostics report produced by the skill."""

    # Metric categories
    metric_results: dict[str, CategoryResult] = field(default_factory=dict)
    # Log results
    logs: LogResult = field(default_factory=LogResult)
    # Aggregated
    bottlenecks: list[Bottleneck] = field(default_factory=list)
    actions: list[RecommendedAction] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    discovered_metrics: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Serialise to a JSON-friendly dict."""
        from dataclasses import asdict
        return asdict(self)
