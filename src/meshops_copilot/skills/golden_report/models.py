"""Domain models for the golden_report skill.

Mirrors the flat-signal pattern from ``data_product_discovery`` but
tailored for Superset dashboards.  All signals are intentionally flat so
the scorer can apply weights without introspecting nested structures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


# ── Category ──────────────────────────────────────────────────────────────────

class Category(str, Enum):
    """Classification bucket for a scored dashboard."""

    GOLDEN = "golden_candidate"
    NEEDS_WORK = "needs_work"
    ANTI_GOLDEN = "anti_golden"


# ── Raw signals ───────────────────────────────────────────────────────────────

@dataclass
class DashboardSignals:
    """All signals collected for a single dashboard from Superset APIs.

    Signals are flat so the scorer can apply weighted normalisation
    directly.
    """

    dashboard_id: int
    title: str
    url: str = ""

    # ── Usage (from /api/v1/log/) ──────────────────────────────────────────
    view_count_30d: int = 0
    unique_viewers_30d: int = 0
    active_weeks_30d: int = 0          # 0-4 distinct ISO-weeks with views

    # ── Ownership & metadata (from dashboard object) ──────────────────────
    owners: list[str] = field(default_factory=list)
    has_description: bool = False
    tags: list[str] = field(default_factory=list)
    published: bool = False
    certified: bool = False
    certified_by: str = ""

    # ── Stability ─────────────────────────────────────────────────────────
    changed_on: str = ""               # ISO-8601 timestamp
    days_since_change: int = 0

    # ── Performance (from /api/v1/query/) ─────────────────────────────────
    chart_count: int = 0
    chart_ids: list[int] = field(default_factory=list)
    median_query_duration_ms: float = 0.0
    p95_query_duration_ms: float = 0.0
    error_rate: float = 0.0            # 0.0 – 1.0

    # ── Dataset quality ───────────────────────────────────────────────────
    dataset_ids: list[int] = field(default_factory=list)
    certified_dataset_fraction: float = 0.0   # 0.0 – 1.0

    # ── Collection health ─────────────────────────────────────────────────
    collection_errors: list[str] = field(default_factory=list)

    @property
    def has_owner(self) -> bool:
        return bool(self.owners)

    @property
    def display_name(self) -> str:
        return self.title or f"Dashboard {self.dashboard_id}"


# ── Scored candidate ─────────────────────────────────────────────────────────

@dataclass
class GoldenCandidate:
    """A dashboard scored and categorised for golden report candidacy."""

    dashboard_id: int
    title: str
    score: float                       # 0.0 – 1.0
    category: Category
    signals: DashboardSignals
    score_breakdown: dict[str, float] = field(default_factory=dict)
    gaps: list[str] = field(default_factory=list)
    justification: str = ""


# ── Duplicate pair ────────────────────────────────────────────────────────────

@dataclass
class DuplicatePair:
    """Two dashboards with high chart overlap, candidates for merging."""

    dashboard_a_id: int
    dashboard_a_title: str
    dashboard_b_id: int
    dashboard_b_title: str
    shared_charts: list[int] = field(default_factory=list)
    jaccard_similarity: float = 0.0
    recommendation: str = ""
